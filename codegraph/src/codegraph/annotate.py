"""Durable, human/agent-authored graph additions.

This is what backs "add to graph this location".  A parser cannot see that a
function is *the retry boundary*, or that two modules are two halves of one
protocol.  Someone has to say so.

The hard requirement is **durability**.  The PostToolUse hook rebuilds the graph
on every edit; an annotation stored only in the graph would be erased within
seconds of being made.  So annotations live in their own append-only file
(``.codegraph/annotations.jsonl``) and are re-applied after every build.  The
graph is a derived artifact; the annotation log is the source of truth.

Append-only, one JSON object per line, because the failure mode of a rewritten
file is losing everything and the failure mode of an append is one bad line.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import ANNOTATION_ORIGIN
from .model import Graph, normalise_path

ANNOTATIONS_FILENAME = "annotations.jsonl"


@dataclass(slots=True)
class Annotation:
    """One asserted fact about a location or a relationship."""

    id: str
    kind: str                      # note | link | tag
    target: str                    # file path, "file:line", node id, or symbol
    text: str = ""
    relation: str = "annotates"
    links_to: str = ""             # for kind="link"
    tags: list[str] = field(default_factory=list)
    root: str = ""
    created_at: float = 0.0
    author: str = "agent"
    revoked: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class AnnotationStore:
    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / ANNOTATIONS_FILENAME

    def append(self, annotation: Annotation) -> Annotation:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(annotation.to_json() + "\n")
        return annotation

    def all(self) -> list[Annotation]:
        """Read the log, newest wins, revoked entries removed.

        A malformed line is skipped rather than fatal - one corrupt append must
        not make every prior annotation unreadable.
        """
        if not self.path.exists():
            return []
        known = {f for f in Annotation.__slots__}
        by_id: dict[str, Annotation] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            annotation = Annotation(**{k: v for k, v in raw.items() if k in known})
            by_id[annotation.id] = annotation
        return [a for a in by_id.values() if not a.revoked]

    def revoke(self, annotation_id: str) -> bool:
        for existing in self.all():
            if existing.id == annotation_id:
                existing.revoked = True
                self.append(existing)
                return True
        return False

    def next_id(self) -> str:
        return f"ann_{int(time.time() * 1000):x}"


def clear_annotations(graph: Graph) -> int:
    dropped = graph.drop_edges(lambda edge: edge.get("_origin") == ANNOTATION_ORIGIN)
    for node_id in [n for n, node in graph.nodes.items() if node.get("_origin") == ANNOTATION_ORIGIN]:
        del graph.nodes[node_id]
    graph.invalidate()
    return dropped


def apply(graph: Graph, annotations: list[Annotation]) -> dict[str, Any]:
    """Re-apply annotations onto a freshly built graph.

    Runs after every build.  An annotation whose target no longer resolves is
    reported as *orphaned* rather than dropped: the code moved, the assertion
    may still be true, and silently discarding it would lose the one thing in
    the graph a parser could not recover.
    """
    clear_annotations(graph)
    applied, orphaned = 0, []

    for annotation in annotations:
        anchors = _resolve(graph, annotation.target)
        if not anchors:
            orphaned.append({"id": annotation.id, "target": annotation.target,
                             "text": annotation.text[:80]})
            continue

        if annotation.kind == "link":
            partners = _resolve(graph, annotation.links_to)
            if not partners:
                orphaned.append({"id": annotation.id, "target": annotation.links_to,
                                 "text": "link target unresolved"})
                continue
            for anchor in anchors[:4]:
                for partner in partners[:4]:
                    graph.add_edge(
                        anchor, partner, annotation.relation or "relates_to",
                        confidence="ASSERTED",
                        origin=ANNOTATION_ORIGIN,
                        annotation_id=annotation.id,
                        evidence=annotation.text or "asserted via codegraph add",
                        weight=1.0,
                    )
            applied += 1
            continue

        # note / tag: a first-class node so the assertion is searchable, not a
        # string buried in an attribute nothing indexes.
        node_id = f"note::{annotation.id}"
        graph.add_node(
            node_id,
            label=_headline(annotation.text) or annotation.id,
            file_type="annotation",
            kind=annotation.kind,
            text=annotation.text,
            tags=annotation.tags,
            author=annotation.author,
            created_at=annotation.created_at,
            root=annotation.root or (graph.nodes[anchors[0]].get("root", "") if anchors else ""),
            source_file=_target_file(annotation.target),
            _origin=ANNOTATION_ORIGIN,
        )
        for anchor in anchors[:6]:
            graph.add_edge(
                node_id, anchor, annotation.relation or "annotates",
                confidence="ASSERTED",
                origin=ANNOTATION_ORIGIN,
                annotation_id=annotation.id,
                weight=1.0,
            )
        applied += 1

    return {"applied": applied, "orphaned": orphaned}


def _resolve(graph: Graph, target: str) -> list[str]:
    """Resolve an annotation target to node ids.

    Accepts a node id, ``path/file.py``, ``path/file.py:120`` or a symbol name.
    With a line number, the nearest definition *at or above* that line wins -
    which is how a location inside a function body resolves to the function.
    """
    if not target:
        return []
    if target in graph.nodes:
        return [target]

    path_part, _, line_part = target.rpartition(":")
    if path_part and line_part.isdigit():
        in_file = graph.nodes_in_file(path_part)
        if in_file:
            wanted = int(line_part)
            scored = []
            for node_id in in_file:
                line = _line_of(graph.nodes[node_id])
                if line is not None and line <= wanted:
                    # Definitions sort ahead of prose at the same distance.
                    # Backends mint nodes for docstrings and comments, which sit
                    # a line or two *below* the function they document - so the
                    # nearest node to a line inside a body is often the
                    # docstring, and anchoring there attaches the note to a
                    # blob of text rather than to the code it is about.
                    scored.append((0 if is_definition(graph.nodes[node_id]) else 1,
                                   wanted - line, node_id))
            if scored:
                scored.sort()
                return [scored[0][2]]
            return in_file[:1]

    in_file = graph.nodes_in_file(target)
    if in_file:
        return in_file
    return graph.find_symbol(target, limit=6)


def is_definition(node: dict[str, Any]) -> bool:
    """Is this node a real code definition rather than extracted prose?

    Backends turn docstrings, comments and rationale markers into first-class
    nodes. Useful in the graph, wrong as an anchor or as a "this file defines"
    entry.
    """
    if node.get("_callable") or node.get("_callable_class"):
        return True
    if node.get("kind") in ("class", "function", "method", "interface", "struct"):
        return True
    label = str(node.get("label") or "")
    # A label with spaces is a sentence a parser lifted out of a comment.
    return bool(label) and " " not in label


def _line_of(node: dict[str, Any]) -> int | None:
    raw = str(node.get("source_location") or "").lstrip("Ll")
    head = raw.split("-")[0].strip()
    return int(head) if head.isdigit() else None


def _target_file(target: str) -> str:
    path_part, _, line_part = target.rpartition(":")
    candidate = path_part if (path_part and line_part.isdigit()) else target
    return normalise_path(candidate) if "/" in candidate or "." in candidate else ""


def _headline(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
