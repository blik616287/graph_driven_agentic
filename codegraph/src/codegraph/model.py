"""The unified graph model.

The on-disk format is networkx node-link JSON - the same shape graphify emits -
so any graphify tooling can read a codegraph graph and vice versa::

    {"directed": bool, "multigraph": bool, "graph": {...},
     "nodes": [{"id", "label", "source_file", "source_location", ...}],
     "links": [{"source", "target", "relation", "confidence", ...}]}

This module deliberately does *not* depend on networkx.  Serving a graph and
answering "what is near this file" are dict lookups; pulling in a graph library
(and its numpy transitive dependency) just to do that would mean the MCP server
and the agent hooks could no longer run on a bare Python.

Conventions this module adds on top of the base format:

``root``          which codebase a node came from
``_origin``       ``ast`` (parsed), ``codegraph-synthetic`` (inferred across
                  roots), or ``codegraph-annotation`` (asserted by a human/agent)
``confidence``    ``EXTRACTED`` / ``INFERRED`` / ``SYNTHETIC`` / ``ASSERTED``
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAMESPACE_SEP = "::"

# Edges whose direction means "A depends on B".  Used when walking a blast
# radius: to find what breaks when B changes, follow these backwards.
DEPENDENCY_RELATIONS = frozenset({"calls", "imports", "inherits", "mixes_in", "uses", "depends_on"})


def namespaced(root: str, node_id: str) -> str:
    """Qualify a backend node id with its root.

    Without this, two roots that both define ``Config`` merge into one node and
    the graph asserts a relationship nobody wrote.
    """
    if NAMESPACE_SEP in node_id:
        return node_id
    return f"{root}{NAMESPACE_SEP}{node_id}"


def split_namespace(node_id: str) -> tuple[str, str]:
    root, sep, rest = node_id.partition(NAMESPACE_SEP)
    return (root, rest) if sep else ("", node_id)


def normalise_path(path: str) -> str:
    """Canonical form for path keys: forward slashes, no leading ``./``."""
    cleaned = str(path).replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


@dataclass(slots=True)
class Graph:
    """Nodes, edges, and the indexes that make lookups cheap."""

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    _out: dict[str, list[int]] | None = field(default=None, repr=False)
    _in: dict[str, list[int]] | None = field(default=None, repr=False)
    _by_file: dict[str, list[str]] | None = field(default=None, repr=False)
    _by_label: dict[str, list[str]] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: Path | str) -> Graph:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_node_link(raw)

    @classmethod
    def from_node_link(cls, raw: dict[str, Any]) -> Graph:
        # Accept both spellings: networkx renamed "links" to "edges" in 3.4.
        link_key = "links" if "links" in raw else "edges"
        nodes: dict[str, dict[str, Any]] = {}
        for entry in raw.get("nodes", []):
            node_id = entry.get("id")
            if node_id:
                nodes[node_id] = dict(entry)
        edges = [dict(entry) for entry in raw.get(link_key, [])]
        meta = dict(raw.get("graph", {}) or {})
        for key in ("built_at_commit", "built_at", "codegraph"):
            if key in raw:
                meta[key] = raw[key]
        return cls(nodes=nodes, edges=edges, meta=meta)

    def to_node_link(self) -> dict[str, Any]:
        return {
            "directed": True,
            "multigraph": False,
            "graph": self.meta,
            "nodes": list(self.nodes.values()),
            "links": self.edges,
        }

    def save(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a hook rebuilding the graph must never leave a
        # half-written file behind for the MCP server to read.
        staging = target.with_suffix(target.suffix + ".tmp")
        staging.write_text(json.dumps(self.to_node_link(), indent=1), encoding="utf-8")
        staging.replace(target)
        return target

    # -------------------------------------------------------------- indexes
    def _build_indexes(self) -> None:
        out: dict[str, list[int]] = defaultdict(list)
        incoming: dict[str, list[int]] = defaultdict(list)
        by_file: dict[str, list[str]] = defaultdict(list)
        by_label: dict[str, list[str]] = defaultdict(list)

        for position, edge in enumerate(self.edges):
            out[edge["source"]].append(position)
            incoming[edge["target"]].append(position)
        for node_id, node in self.nodes.items():
            source_file = node.get("source_file")
            if source_file:
                by_file[normalise_path(source_file)].append(node_id)
            label = node.get("label") or node.get("norm_label")
            if label:
                by_label[str(label).lower()].append(node_id)

        self._out, self._in, self._by_file, self._by_label = out, incoming, by_file, by_label

    def invalidate(self) -> None:
        self._out = self._in = self._by_file = self._by_label = None

    @property
    def out_index(self) -> dict[str, list[int]]:
        if self._out is None:
            self._build_indexes()
        return self._out  # type: ignore[return-value]

    @property
    def in_index(self) -> dict[str, list[int]]:
        if self._in is None:
            self._build_indexes()
        return self._in  # type: ignore[return-value]

    @property
    def file_index(self) -> dict[str, list[str]]:
        if self._by_file is None:
            self._build_indexes()
        return self._by_file  # type: ignore[return-value]

    @property
    def label_index(self) -> dict[str, list[str]]:
        if self._by_label is None:
            self._build_indexes()
        return self._by_label  # type: ignore[return-value]

    # --------------------------------------------------------------- reads
    def node(self, node_id: str) -> dict[str, Any] | None:
        return self.nodes.get(node_id)

    def degree(self, node_id: str) -> int:
        return len(self.out_index.get(node_id, ())) + len(self.in_index.get(node_id, ()))

    def outgoing(self, node_id: str) -> list[dict[str, Any]]:
        return [self.edges[i] for i in self.out_index.get(node_id, ())]

    def incoming(self, node_id: str) -> list[dict[str, Any]]:
        return [self.edges[i] for i in self.in_index.get(node_id, ())]

    def neighbours(self, node_id: str) -> list[tuple[str, dict[str, Any], str]]:
        """``(other_id, edge, direction)`` for every edge touching ``node_id``."""
        found = [(edge["target"], edge, "out") for edge in self.outgoing(node_id)]
        found += [(edge["source"], edge, "in") for edge in self.incoming(node_id)]
        return found

    def nodes_in_file(self, file_path: str) -> list[str]:
        """Nodes defined in a file.

        Falls back to a suffix match so a caller holding an absolute path, or a
        path relative to a different directory, still resolves.  Exact first -
        a suffix match is a guess and should never shadow a certain answer.
        """
        wanted = normalise_path(file_path)
        exact = self.file_index.get(wanted)
        if exact:
            return list(exact)
        matches: list[str] = []
        for indexed, node_ids in self.file_index.items():
            if indexed.endswith(wanted) or wanted.endswith(indexed):
                matches.extend(node_ids)
        return matches

    def find_symbol(self, name: str, limit: int = 20) -> list[str]:
        lowered = name.lower()
        exact = list(self.label_index.get(lowered, ()))
        if len(exact) >= limit:
            return exact[:limit]
        partial = [
            node_id
            for label, ids in self.label_index.items()
            if lowered in label and label != lowered
            for node_id in ids
        ]
        return (exact + partial)[:limit]

    def files(self) -> list[str]:
        return sorted(self.file_index)

    def roots(self) -> list[str]:
        return sorted({node.get("root", "") for node in self.nodes.values()} - {""})

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.nodes.values())

    # -------------------------------------------------------------- writes
    def add_node(self, node_id: str, **attributes: Any) -> dict[str, Any]:
        existing = self.nodes.get(node_id)
        if existing is None:
            existing = {"id": node_id}
            self.nodes[node_id] = existing
        existing.update(attributes)
        self.invalidate()
        return existing

    def add_edge(
        self,
        source: str,
        target: str,
        relation: str,
        *,
        confidence: str = "EXTRACTED",
        origin: str = "ast",
        **attributes: Any,
    ) -> dict[str, Any]:
        edge = {
            "source": source,
            "target": target,
            "relation": relation,
            "confidence": confidence,
            "_origin": origin,
            **attributes,
        }
        self.edges.append(edge)
        self.invalidate()
        return edge

    def has_edge(self, source: str, target: str, relation: str) -> bool:
        return any(
            edge["target"] == target and edge.get("relation") == relation
            for edge in self.outgoing(source)
        )

    def drop_edges(self, predicate) -> int:
        """Remove edges matching ``predicate``.  Returns how many went.

        Rebuilds discard and recompute derived edges; keeping stale synthetic
        links across a rebuild would let a deleted symbol keep its relationships
        forever.
        """
        before = len(self.edges)
        self.edges = [edge for edge in self.edges if not predicate(edge)]
        self.invalidate()
        return before - len(self.edges)

    # -------------------------------------------------------------- merging
    def namespace_to(self, root: str) -> Graph:
        """Return a copy with every id qualified by ``root``."""
        mapping = {node_id: namespaced(root, node_id) for node_id in self.nodes}
        nodes = {}
        for node_id, node in self.nodes.items():
            clone = dict(node)
            clone["id"] = mapping[node_id]
            clone["root"] = root
            clone.setdefault("_origin", "ast")
            nodes[mapping[node_id]] = clone
        edges = []
        for edge in self.edges:
            # Edges pointing outside this graph's node set are dangling - the
            # backend resolved a call to something it never indexed.  Keep them
            # namespaced so the dangling end stays attributable to this root.
            clone = dict(edge)
            source_id, target_id = edge.get("source"), edge.get("target")
            clone["source"] = mapping.get(source_id, namespaced(root, str(source_id)))
            clone["target"] = mapping.get(target_id, namespaced(root, str(target_id)))
            clone["root"] = root
            clone.setdefault("_origin", "ast")
            edges.append(clone)
        return Graph(nodes=nodes, edges=edges, meta=dict(self.meta))

    @classmethod
    def merge(cls, graphs: Iterable[Graph], meta: dict[str, Any] | None = None) -> Graph:
        """Union of already-namespaced graphs.

        Namespacing makes this a plain union: ids cannot collide across roots,
        so there is no reconciliation step and no chance of silently fusing two
        unrelated symbols.
        """
        merged = cls(meta=dict(meta or {}))
        for graph in graphs:
            merged.nodes.update(graph.nodes)
            merged.edges.extend(graph.edges)
        merged.invalidate()
        return merged

    # ------------------------------------------------------------ traversal
    def blast_radius(self, node_id: str, depth: int = 2, limit: int = 60) -> list[dict[str, Any]]:
        """What might break if ``node_id`` changes.

        Walks *dependency edges backwards*: callers of a function, importers of
        a module, subclasses of a class.  Breadth-first so the closest - and
        therefore most likely affected - nodes come out first, and capped,
        because a hub node's true blast radius is "most of the repo" and that is
        not a useful answer to hand an agent.
        """
        seen = {node_id}
        frontier = [node_id]
        found: list[dict[str, Any]] = []
        for distance in range(1, depth + 1):
            next_frontier: list[str] = []
            for current in frontier:
                for edge in self.incoming(current):
                    if edge.get("relation") not in DEPENDENCY_RELATIONS:
                        continue
                    dependent = edge["source"]
                    if dependent in seen:
                        continue
                    seen.add(dependent)
                    next_frontier.append(dependent)
                    node = self.nodes.get(dependent)
                    found.append(
                        {
                            "id": dependent,
                            "label": (node or {}).get("label", dependent),
                            "source_file": (node or {}).get("source_file", ""),
                            "source_location": (node or {}).get("source_location", ""),
                            "relation": edge.get("relation"),
                            "distance": distance,
                            "confidence": edge.get("confidence", "EXTRACTED"),
                        }
                    )
                    if len(found) >= limit:
                        return found
            frontier = next_frontier
            if not frontier:
                break
        return found

    def shortest_path(self, source: str, target: str, max_depth: int = 8) -> list[str]:
        """Undirected BFS.  Empty list when unreachable.

        Undirected on purpose: "how do these two things connect" is a question
        about the shape of the code, and insisting on edge direction usually
        answers "they do not" for two nodes that plainly do.
        """
        if source == target:
            return [source]
        if source not in self.nodes or target not in self.nodes:
            return []
        previous: dict[str, str] = {source: ""}
        frontier = [source]
        for _ in range(max_depth):
            next_frontier: list[str] = []
            for current in frontier:
                for other, _edge, _direction in self.neighbours(current):
                    if other in previous:
                        continue
                    previous[other] = current
                    if other == target:
                        path = [target]
                        while previous[path[-1]]:
                            path.append(previous[path[-1]])
                        return list(reversed(path))
                    next_frontier.append(other)
            if not next_frontier:
                break
            frontier = next_frontier
        return []

    def hubs(self, limit: int = 15, root: str | None = None) -> list[dict[str, Any]]:
        """Most-connected nodes: the parts of the system everything touches."""
        scored = [
            {
                "id": node_id,
                "label": node.get("label", node_id),
                "root": node.get("root", ""),
                "source_file": node.get("source_file", ""),
                "degree": self.degree(node_id),
            }
            for node_id, node in self.nodes.items()
            if root is None or node.get("root") == root
        ]
        scored.sort(key=lambda entry: -entry["degree"])
        return scored[:limit]

    def stats(self) -> dict[str, Any]:
        by_root: dict[str, int] = defaultdict(int)
        by_relation: dict[str, int] = defaultdict(int)
        by_origin: dict[str, int] = defaultdict(int)
        for node in self.nodes.values():
            by_root[node.get("root", "?")] += 1
        for edge in self.edges:
            by_relation[edge.get("relation", "?")] += 1
            by_origin[edge.get("_origin", "?")] += 1
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "files": len(self.file_index),
            "roots": dict(sorted(by_root.items())),
            "relations": dict(sorted(by_relation.items(), key=lambda kv: -kv[1])),
            "edge_origins": dict(sorted(by_origin.items(), key=lambda kv: -kv[1])),
            "built_at": self.meta.get("built_at"),
            "backend": self.meta.get("backend"),
        }
