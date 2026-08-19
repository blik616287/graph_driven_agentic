"""code-review-graph backend (https://github.com/tirth8205/code-review-graph).

code-review-graph stores its graph in SQLite under ``.code-review-graph/`` and
exports JSON on demand, so the adapter is: build (or incrementally update),
export, translate into codegraph's node-link shape.

The translation is where the interesting work is.  The two tools disagree about
vocabulary - crg emits ``type``/``name``/``file``/``line``, graphify emits
``label``/``source_file``/``source_location`` - and normalising at the edge is
what lets everything downstream stay backend-agnostic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..model import Graph, normalise_path
from .base import Backend, BackendError, BackendUnavailable

# crg edge names -> codegraph's vocabulary (which follows graphify's).
RELATION_ALIASES = {
    "call": "calls",
    "calls": "calls",
    "import": "imports",
    "imports": "imports",
    "inherit": "inherits",
    "inherits": "inherits",
    "extends": "inherits",
    "implements": "inherits",
    "contains": "contains",
    "defines": "contains",
    "tests": "tests",
    "references": "uses",
}


class CodeReviewGraphBackend(Backend):
    name = "code-review-graph"

    def probe(self) -> str:
        if self._which("code-review-graph"):
            return "code-review-graph ready (on PATH)"
        if self._module_available("code_review_graph"):
            return f"code-review-graph ready (module under {self.python})"
        raise BackendUnavailable(
            "code-review-graph is not installed - run `make install-crg`"
        )

    def _command(self) -> list[str]:
        if self._which("code-review-graph"):
            return ["code-review-graph"]
        return [self.python, "-m", "code_review_graph"]

    def extract(self, path: Path, *, out_dir: Path, incremental: bool = False) -> Graph:
        path = path.resolve()
        if not path.is_dir():
            raise BackendError(f"not a directory: {path}")
        out_dir.mkdir(parents=True, exist_ok=True)
        base = self._command()

        self._run([*base, "update" if incremental else "build"], cwd=path)

        # `visualize --format json` prints to stdout on some versions and writes
        # a file on others; handle both rather than pinning a version.
        raw = self._run([*base, "visualize", "--format", "json"], cwd=path)
        payload = _first_json_object(raw)
        if payload is None:
            for candidate in (
                path / ".code-review-graph" / "graph.json",
                path / "graph.json",
                out_dir / "graph.json",
            ):
                if candidate.exists():
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                    break
        if payload is None:
            raise BackendError("code-review-graph produced no JSON export")

        return translate(payload, source_root=path)


def translate(payload: dict[str, Any], source_root: Path | None = None) -> Graph:
    """Convert a code-review-graph JSON export into codegraph's shape."""
    graph = Graph(meta={"backend": CodeReviewGraphBackend.name})
    link_key = next((k for k in ("links", "edges", "relationships") if k in payload), "edges")

    for entry in payload.get("nodes", []):
        node_id = str(entry.get("id") or entry.get("qualified_name") or entry.get("name") or "")
        if not node_id:
            continue
        source_file = entry.get("file") or entry.get("path") or entry.get("source_file") or ""
        line = entry.get("line") or entry.get("start_line") or entry.get("lineno")
        graph.add_node(
            node_id,
            label=entry.get("name") or entry.get("label") or node_id,
            source_file=normalise_path(_relative(source_file, source_root)),
            source_location=f"L{line}" if line else entry.get("source_location", ""),
            kind=entry.get("type") or entry.get("kind") or "symbol",
            file_type="code",
            _origin="ast",
        )

    for entry in payload.get(link_key, []):
        source = str(entry.get("source") or entry.get("from") or "")
        target = str(entry.get("target") or entry.get("to") or "")
        if not source or not target:
            continue
        raw_relation = str(entry.get("relationship") or entry.get("type") or entry.get("relation") or "uses")
        graph.add_edge(
            source,
            target,
            RELATION_ALIASES.get(raw_relation.lower(), raw_relation.lower()),
            confidence=str(entry.get("confidence", "EXTRACTED")).upper(),
            origin="ast",
        )
    return graph


def _relative(file_path: str, root: Path | None) -> str:
    if not file_path or root is None:
        return file_path or ""
    try:
        return str(Path(file_path).resolve().relative_to(root))
    except (ValueError, OSError):
        return file_path


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of mixed CLI output.

    CLIs interleave progress lines with their payload; scanning for the first
    balanced ``{...}`` is more robust than assuming stdout is pure JSON.
    """
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None
