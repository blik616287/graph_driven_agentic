"""Build orchestration.

The pipeline, in order, and each step exists for a reason:

1. **Extract** each root with the configured backend (AST only, deterministic).
2. **Normalise paths** so every ``source_file`` is workspace-relative.  This is
   the step that makes the hooks work at all: an agent edits
   ``example_src/marketd/core/book.py``, and unless the graph indexes that exact
   string, no lookup will ever match.
3. **Namespace and merge** the per-root graphs.
4. **Link** roots with synthetic edges.
5. **Re-apply annotations**, because a rebuild would otherwise erase every
   hand-asserted fact.
6. **Write** the graph and a small detection index, atomically.

Steps 4-6 are cheap and pure. Only step 1 costs anything, which is why
``incremental=True`` exists: it hands the backend its own change detection and
re-runs everything after it regardless.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .annotate import AnnotationStore
from .annotate import apply as apply_annotations
from .backends import get_backend
from .backends.base import BackendError
from .config import Config
from .model import Graph, normalise_path
from .synthetic import link_roots

GRAPH_FILENAME = "graph.json"
INDEX_FILENAME = "index.json"
STATE_FILENAME = "state.json"
ROOTS_DIRNAME = "roots"

# Symbols too generic to be worth matching a prompt against.
INDEX_STOPWORDS = frozenset({
    "main", "run", "test", "tests", "setup", "init", "self", "true", "false", "none",
    "get", "set", "add", "put", "new", "old", "all", "any", "key", "val", "value",
    "data", "item", "list", "dict", "type", "name", "path", "file", "line", "text",
})


@dataclass(slots=True)
class BuildReport:
    graph_path: Path
    nodes: int = 0
    edges: int = 0
    roots: dict[str, dict[str, Any]] = field(default_factory=dict)
    synthetic: dict[str, int] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    duration: float = 0.0
    incremental: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph": str(self.graph_path),
            "nodes": self.nodes,
            "edges": self.edges,
            "roots": self.roots,
            "synthetic": self.synthetic,
            "annotations": self.annotations,
            "errors": self.errors,
            "duration_seconds": round(self.duration, 3),
            "incremental": self.incremental,
        }

    def summary(self) -> str:
        parts = [f"{self.nodes} nodes", f"{self.edges} edges"]
        if self.roots:
            parts.append(
                "roots: " + ", ".join(f"{n}({d.get('nodes', 0)})" for n, d in self.roots.items())
            )
        synthetic_total = sum(self.synthetic.values())
        if synthetic_total:
            parts.append(f"{synthetic_total} synthetic")
        if self.annotations.get("applied"):
            parts.append(f"{self.annotations['applied']} annotations")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return " | ".join(parts) + f" | {self.duration:.2f}s"


def graph_path(workspace: Path) -> Path:
    return Config.dir_for(workspace) / GRAPH_FILENAME


def index_path(workspace: Path) -> Path:
    return Config.dir_for(workspace) / INDEX_FILENAME


def state_path(workspace: Path) -> Path:
    return Config.dir_for(workspace) / STATE_FILENAME


def build(
    workspace: Path,
    config: Config | None = None,
    *,
    roots: list[str] | None = None,
    incremental: bool = False,
    backend_python: str | None = None,
) -> BuildReport:
    """Run the full pipeline and write the graph."""
    started = time.perf_counter()
    workspace = workspace.resolve()
    config = config or Config.load(workspace)
    out_dir = Config.dir_for(workspace)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = BuildReport(graph_path=graph_path(workspace), incremental=incremental)

    selected = [r for r in config.roots if roots is None or r.name in roots]
    if not selected:
        raise ValueError("no roots selected - add one with `codegraph add-root <path>`")

    backend = get_backend(config.backend, python=backend_python)
    per_root: list[Graph] = []

    for root in selected:
        root_dir = root.resolve(workspace)
        cached = out_dir / ROOTS_DIRNAME / f"{root.name}.json"
        if not root_dir.is_dir():
            report.errors.append(f"root {root.name!r}: {root_dir} is not a directory")
            continue
        try:
            raw = backend.extract(
                root_dir,
                out_dir=out_dir / ROOTS_DIRNAME / root.name,
                incremental=incremental,
            )
        except BackendError as exc:
            report.errors.append(f"root {root.name!r}: {exc}")
            # A backend failure must not wipe a root that built fine before.
            # Fall back to the last good graph so a broken build degrades to a
            # stale one rather than to nothing.
            if cached.exists():
                per_root.append(Graph.load(cached))
                report.roots[root.name] = {"stale": True, "nodes": len(per_root[-1].nodes)}
            continue

        _rewrite_paths(raw, workspace=workspace, root_dir=root_dir)
        namespaced_graph = raw.namespace_to(root.name)
        namespaced_graph.save(cached)
        per_root.append(namespaced_graph)
        report.roots[root.name] = {
                "path": (
                    str(root_dir.relative_to(workspace))
                    if _under(root_dir, workspace)
                    else str(root_dir)
                ),
            "nodes": len(namespaced_graph.nodes),
            "edges": len(namespaced_graph.edges),
            "stale": False,
        }

    # Roots that were not rebuilt this run still belong in the merged graph.
    for root in config.roots:
        if roots is not None and root.name in roots:
            continue
        if root.name in report.roots:
            continue
        cached = out_dir / ROOTS_DIRNAME / f"{root.name}.json"
        if cached.exists():
            per_root.append(Graph.load(cached))
            report.roots[root.name] = {"reused": True, "nodes": len(per_root[-1].nodes)}

    if not per_root:
        raise BackendError(
            "every root failed to extract; run `codegraph doctor` to check the backend"
            + (f" ({report.errors[0]})" if report.errors else "")
        )

    merged = Graph.merge(
        per_root,
        meta={
            "backend": config.backend,
            "codegraph_version": __version__,
            "built_at": time.time(),
            "workspace": str(workspace),
            "roots": [r.name for r in config.roots],
        },
    )

    report.synthetic = link_roots(merged, config.synthetic)
    report.annotations = apply_annotations(merged, AnnotationStore(out_dir).all())

    merged.save(report.graph_path)
    write_index(workspace, merged, config)

    # Counts and duration are filled in *before* the state file is written -
    # it is a serialisation of the report, so anything set afterwards would be
    # missing from it.
    report.nodes = len(merged.nodes)
    report.edges = len(merged.edges)
    report.duration = time.perf_counter() - started
    _write_state(workspace, report)
    return report


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _rewrite_paths(graph: Graph, *, workspace: Path, root_dir: Path) -> None:
    """Make every ``source_file`` workspace-relative.

    Backends report paths relative to wherever they ran, which is not where the
    agent is standing.  Resolution is by *existence on disk*, tried against the
    root then the workspace, so the answer is verified rather than guessed.
    Paths that resolve to neither are left untouched - a wrong rewrite is worse
    than an unrewritten one, because it would index a file that does not exist.
    """
    cache: dict[str, str] = {}

    def rewrite(raw: str) -> str:
        if raw in cache:
            return cache[raw]
        cleaned = normalise_path(raw)
        resolved = cleaned
        for candidate in (root_dir / cleaned, workspace / cleaned, Path(cleaned)):
            try:
                if candidate.exists():
                    absolute = candidate.resolve()
                    if _under(absolute, workspace):
                        resolved = normalise_path(str(absolute.relative_to(workspace)))
                    else:
                        resolved = normalise_path(str(absolute))
                    break
            except OSError:
                continue
        cache[raw] = resolved
        return resolved

    for node in graph.nodes.values():
        source_file = node.get("source_file")
        if source_file:
            node["source_file"] = rewrite(str(source_file))
    for edge in graph.edges:
        source_file = edge.get("source_file")
        if source_file:
            edge["source_file"] = rewrite(str(source_file))
    graph.invalidate()


def write_index(workspace: Path, graph: Graph, config: Config) -> Path:
    """Write the small detection index the prompt hook reads.

    Separate from ``graph.json`` on purpose.  The UserPromptSubmit hook runs on
    *every* prompt and its first job is to decide "does this prompt touch the
    codebase at all?" - which for the overwhelming majority of prompts is "no".
    Answering that from a compact index keeps the common case to a few
    milliseconds; only a hit pays to load the full graph.
    """
    files = sorted(graph.file_index)
    basenames: dict[str, list[str]] = {}
    for path in files:
        basenames.setdefault(path.rsplit("/", 1)[-1], []).append(path)

    # Only identifier-shaped labels are worth matching against a prompt.
    # Backends also mint nodes from docstrings and comments, whose labels are
    # whole sentences - they belong in the graph, but nobody types one, and
    # indexing them makes every prompt containing a common word a false hit.
    symbols = sorted({
        label.lower()
        for label in (str(node.get("label", "")).strip() for node in graph.nodes.values())
        if 4 <= len(label) <= 40
        and " " not in label
        and label.lower() not in INDEX_STOPWORDS
    })

    payload = {
        "version": 1,
        "codegraph_version": __version__,
        "built_at": time.time(),
        "graph": GRAPH_FILENAME,
        "roots": {
            root.name: str(root.resolve(workspace).relative_to(workspace))
            if _under(root.resolve(workspace), workspace)
            else str(root.resolve(workspace))
            for root in config.roots
        },
        "counts": {"nodes": len(graph.nodes), "edges": len(graph.edges), "files": len(files)},
        "files": files,
        "basenames": basenames,
        "symbols": symbols,
    }
    target = index_path(workspace)
    # Callable independently of build() - which is how tests and the install
    # path use it - so it cannot assume the directory already exists.
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".json.tmp")
    staging.write_text(json.dumps(payload), encoding="utf-8")
    staging.replace(target)
    return target


def _write_state(workspace: Path, report: BuildReport) -> None:
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_build": time.time(), **report.to_dict()}, indent=2), encoding="utf-8"
    )


def load_state(workspace: Path) -> dict[str, Any]:
    path = state_path(workspace)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
