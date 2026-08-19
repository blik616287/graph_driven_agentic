"""Turning a prompt into graph context.

This is what the UserPromptSubmit hook runs.  Two phases, split because they
have wildly different costs:

**Detect** (cheap, runs on every prompt).  Does this prompt mention anything in
the indexed codebases?  Answered against ``index.json`` - a flat list of file
paths and symbol names.  Most prompts mention nothing and stop here.

**Assemble** (only on a hit).  Load the graph and build a compact briefing:
what lives in the file, what calls into it, what it would break, and any
human-asserted notes attached to it.

Two rules govern the output, and both come from the same fact - this text is
prepended to a real user's prompt:

*Stay inside the budget.*  Context that crowds out the actual question makes
the agent worse, not better.  Sections are emitted in priority order and
truncated, never summarised into vagueness.

*Never guess silently.*  Synthetic and asserted edges are labelled in the
output, so the agent can tell a parsed call from a heuristic match.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ANNOTATION_ORIGIN, SYNTHETIC_ORIGIN
from .model import Graph, normalise_path

# A path-like token: at least one slash, ending in a plausible file extension.
PATH_PATTERN = re.compile(r"[\w./~-]*[\w-]+/[\w./-]+\.[A-Za-z][A-Za-z0-9]{0,7}\b")
# A bare filename with an extension, e.g. "book.py".
FILENAME_PATTERN = re.compile(r"\b[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7}\b")
# Identifier-shaped tokens: CamelCase, snake_case, or dotted.attribute chains.
IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
# Anything the user explicitly quoted deserves priority.
BACKTICK_PATTERN = re.compile(r"`([^`\n]{2,80})`")

# Words that look like identifiers but are English.
PROSE_WORDS = frozenset({
    "this", "that", "with", "from", "have", "what", "when", "where", "which", "should",
    "would", "could", "please", "there", "their", "them", "then", "than", "into", "about",
    "make", "made", "does", "done", "need", "want", "like", "some", "only", "also", "just",
    "code", "file", "files", "function", "class", "method", "test", "tests", "change",
    "changes", "update", "fix", "bug", "issue", "error", "help", "look", "check", "review",
    "here", "your", "yours", "using", "used", "uses", "work", "works", "working", "call",
    "calls", "called", "read", "write", "line", "lines", "name", "value", "values", "type",
    "types", "return", "returns", "python", "javascript", "typescript", "thanks",
})


@dataclass(slots=True)
class Detection:
    """What a prompt appears to reference."""

    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    quoted: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.files or self.symbols)

    def to_dict(self) -> dict[str, Any]:
        return {"files": self.files, "symbols": self.symbols, "quoted": self.quoted}


class Index:
    """The compact detection index, loaded from ``index.json``."""

    __slots__ = ("basenames", "built_at", "counts", "file_set", "files", "roots", "symbols")

    def __init__(self, payload: dict[str, Any]) -> None:
        self.files: list[str] = payload.get("files", [])
        self.file_set = set(self.files)
        self.basenames: dict[str, list[str]] = payload.get("basenames", {})
        self.symbols: set[str] = set(payload.get("symbols", []))
        self.roots: dict[str, str] = payload.get("roots", {})
        self.counts: dict[str, int] = payload.get("counts", {})
        self.built_at: float = payload.get("built_at", 0.0)

    @classmethod
    def load(cls, path: Path) -> Index:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def resolve_path(self, candidate: str) -> str | None:
        """Map a path written in a prompt onto an indexed path."""
        cleaned = normalise_path(candidate)
        if cleaned in self.file_set:
            return cleaned
        # Absolute or partial paths: match on the longest unique suffix.
        matches = [indexed for indexed in self.files if indexed.endswith(cleaned)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return min(matches, key=len)
        return None

    def resolve_basename(self, name: str) -> list[str]:
        return self.basenames.get(name, [])


def detect(prompt: str, index: Index, max_files: int = 6, max_symbols: int = 8) -> Detection:
    """Find references to indexed code in a prompt.

    Ordered by how much the user signalled intent: an explicit path beats a
    backticked name beats a bare identifier that happens to match.
    """
    detection = Detection()
    seen_files: set[str] = set()
    seen_symbols: set[str] = set()

    quoted = [match.strip() for match in BACKTICK_PATTERN.findall(prompt)]
    detection.quoted = quoted[:10]

    # 1. Explicit paths, including inside backticks.
    path_spans: list[str] = []
    for candidate in PATH_PATTERN.findall(prompt):
        path_spans.append(candidate)
        resolved = index.resolve_path(candidate)
        if resolved and resolved not in seen_files:
            seen_files.add(resolved)
            detection.files.append(resolved)

    # Identifier scanning runs against the prompt with paths removed.  Without
    # this, "example_src/marketd/core/book.py" also reports the symbols
    # "example", "marketd" and "core" - directory names are not references.
    residue = prompt
    for span in path_spans:
        residue = residue.replace(span, " ")

    # 2. Bare filenames ("what does book.py do?").
    for candidate in FILENAME_PATTERN.findall(prompt):
        for resolved in index.resolve_basename(candidate):
            if resolved not in seen_files:
                seen_files.add(resolved)
                detection.files.append(resolved)

    # 3. Quoted identifiers - the user pointed at these deliberately.
    for candidate in quoted:
        lowered = candidate.lower()
        if lowered in index.symbols and lowered not in seen_symbols:
            seen_symbols.add(lowered)
            detection.symbols.append(candidate)

    # 4. Bare identifiers that match a known symbol.
    for candidate in IDENTIFIER_PATTERN.findall(residue):
        lowered = candidate.lower()
        if lowered in PROSE_WORDS or lowered in seen_symbols:
            continue
        if lowered in index.symbols:
            seen_symbols.add(lowered)
            detection.symbols.append(candidate)

    detection.files = detection.files[:max_files]
    detection.symbols = detection.symbols[:max_symbols]
    return detection


# --------------------------------------------------------------------------
#  Context assembly
# --------------------------------------------------------------------------

def context_for(
    graph: Graph,
    *,
    files: list[str] | None = None,
    symbols: list[str] | None = None,
    budget: int = 1800,
    depth: int = 2,
) -> str:
    """Render a compact briefing.  Returns "" when there is nothing to say."""
    sections: list[str] = []
    seen_nodes: set[str] = set()

    for file_path in files or []:
        block = _file_section(graph, file_path, seen_nodes)
        if block:
            sections.append(block)

    for symbol in symbols or []:
        block = _symbol_section(graph, symbol, seen_nodes, depth=depth)
        if block:
            sections.append(block)

    if not sections:
        return ""

    notes = _annotation_section(graph, seen_nodes)
    if notes:
        sections.append(notes)
    crossings = _cross_root_section(graph, seen_nodes)
    if crossings:
        sections.append(crossings)

    header = (
        "## codegraph context\n"
        "Structural facts from the deterministic AST graph. "
        "`~` marks inferred or synthetic links; `!` marks human-asserted notes."
    )
    return _fit([header, *sections], budget)


def _file_section(graph: Graph, file_path: str, seen: set[str]) -> str:
    node_ids = graph.nodes_in_file(file_path)
    if not node_ids:
        return ""
    seen.update(node_ids)

    # Prose nodes (docstrings, comments) belong in the graph but not in a list
    # headed "defines:" - they are not definitions, and they crowd out the ones
    # that are.
    from .annotate import is_definition

    definitions = [n for n in node_ids if is_definition(graph.nodes[n])] or node_ids
    ranked = sorted(definitions, key=lambda n: -graph.degree(n))
    lines = [f"### {file_path}"]

    defined = []
    for node_id in ranked[:12]:
        node = graph.nodes[node_id]
        label = node.get("label", node_id)
        where = node.get("source_location", "")
        defined.append(f"{label}{' ' + where if where else ''} (deg {graph.degree(node_id)})")
    lines.append("defines: " + ", ".join(defined))

    # Who depends on this file, from outside it.
    external: dict[str, str] = {}
    for node_id in ranked[:8]:
        for entry in graph.blast_radius(node_id, depth=1, limit=12):
            if entry["source_file"] and entry["source_file"] != file_path:
                external.setdefault(entry["source_file"], entry["relation"])
    if external:
        listed = ", ".join(f"{path} ({relation})" for path, relation in list(external.items())[:8])
        lines.append(f"depended on by: {listed}")

    # What this file reaches out to.
    outbound: dict[str, str] = {}
    for node_id in ranked[:8]:
        for edge in graph.outgoing(node_id):
            target = graph.nodes.get(edge["target"])
            if not target:
                continue
            target_file = target.get("source_file", "")
            if target_file and target_file != file_path:
                outbound.setdefault(target_file, edge.get("relation", "uses"))
    if outbound:
        listed = ", ".join(f"{path} ({relation})" for path, relation in list(outbound.items())[:8])
        lines.append(f"depends on: {listed}")

    return "\n".join(lines)


def _symbol_section(graph: Graph, symbol: str, seen: set[str], depth: int = 2) -> str:
    matches = graph.find_symbol(symbol, limit=3)
    matches = [m for m in matches if m not in seen]
    if not matches:
        return ""

    lines = [f"### {symbol}"]
    for node_id in matches[:2]:
        node = graph.nodes[node_id]
        seen.add(node_id)
        where = node.get("source_location", "")
        location = f"{node.get('source_file', '?')}{' ' + where if where else ''}"
        lines.append(f"{node.get('label', node_id)} - {location} (deg {graph.degree(node_id)})")

        callers = graph.blast_radius(node_id, depth=depth, limit=10)
        if callers:
            rendered = ", ".join(
                f"{entry['label']}{'~' if entry['confidence'] not in ('EXTRACTED',) else ''}"
                f" [{entry['source_file'] or '?'}]"
                for entry in callers[:6]
            )
            lines.append(f"  impacted by a change here: {rendered}")

        uses = [
            graph.nodes[edge["target"]].get("label", edge["target"])
            for edge in graph.outgoing(node_id)
            if edge["target"] in graph.nodes and edge.get("_origin") != SYNTHETIC_ORIGIN
        ]
        if uses:
            lines.append("  uses: " + ", ".join(dict.fromkeys(uses))[:400])
    return "\n".join(lines)


def _annotation_section(graph: Graph, seen: set[str]) -> str:
    """Human-asserted notes attached to anything already in the briefing.

    These come last but matter most: they are the only facts in the graph a
    parser could not have derived.
    """
    found: list[str] = []
    for node_id in list(seen):
        for edge in graph.incoming(node_id):
            if edge.get("_origin") != ANNOTATION_ORIGIN:
                continue
            note = graph.nodes.get(edge["source"])
            if not note:
                continue
            text = note.get("text") or note.get("label", "")
            target = graph.nodes.get(node_id, {}).get("label", node_id)
            entry = f"! {target}: {text}"
            if entry not in found:
                found.append(entry)
    if not found:
        return ""
    return "### asserted notes\n" + "\n".join(found[:8])


def _cross_root_section(graph: Graph, seen: set[str]) -> str:
    """Synthetic links from the briefing's nodes into other roots."""
    found: list[str] = []
    for node_id in list(seen):
        for other, edge, _direction in graph.neighbours(node_id):
            if edge.get("_origin") != SYNTHETIC_ORIGIN:
                continue
            target = graph.nodes.get(other)
            if not target:
                continue
            source_root = graph.nodes.get(node_id, {}).get("root", "")
            target_root = target.get("root", "")
            if source_root == target_root:
                continue
            entry = (
                f"~ {graph.nodes[node_id].get('label', node_id)} [{source_root}] "
                f"{edge.get('relation')} {target.get('label', other)} [{target_root}] "
                f"({edge.get('rule', 'synthetic')})"
            )
            if entry not in found:
                found.append(entry)
    if not found:
        return ""
    return "### cross-root links (synthetic)\n" + "\n".join(found[:8])


def _fit(sections: list[str], budget: int) -> str:
    """Join sections, dropping whole trailing ones rather than truncating mid-fact.

    Budget is in characters, approximated at ~4 chars/token.  A half-written
    fact is worse than an absent one: the agent cannot tell it was cut off.
    """
    limit = max(400, budget * 4)
    out: list[str] = []
    used = 0
    for section in sections:
        cost = len(section) + 2
        if used + cost > limit and out:
            out.append(f"\n_(context truncated at {budget} tokens)_")
            break
        out.append(section)
        used += cost
    return "\n\n".join(out)
