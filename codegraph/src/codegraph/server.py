"""MCP server for a codegraph workspace.

Speaks JSON-RPC 2.0 over stdio, implemented against the standard library only.
That is a deliberate constraint: the server has to start under whatever Python
the coding assistant happens to launch it with, and a dependency on an SDK -
which in turn pulls in pydantic, anyio and friends - is the difference between
"works everywhere" and "works on my machine".

The graph is reloaded whenever ``graph.json``'s mtime changes.  It has to be:
the PostToolUse hook rewrites that file after every edit, and a server holding a
snapshot from session start would confidently answer questions about code that
no longer exists.

Tools fall into three groups:

*read*    stats, context_for_paths, find_symbol, get_node, impact_of,
          shortest_path, list_roots
*write*   add_to_graph, link_locations, revoke_annotation - the interactive
          "add this to the graph" surface, persisted to the annotation log
*admin*   rebuild_graph
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import __version__
from .annotate import Annotation, AnnotationStore
from .annotate import apply as apply_annotations
from .build import graph_path, index_path
from .config import Config, find_workspace
from .context import Index, context_for, detect
from .model import Graph

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-03-26", "2024-11-05"}

# JSON-RPC error codes (spec section 5.1).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """A tool failed in a way the model should see and can act on."""


class Workspace:
    """Holds the graph and reloads it when it changes underneath us."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._graph: Graph | None = None
        self._index: Index | None = None
        self._graph_mtime = 0.0
        self._index_mtime = 0.0

    @property
    def graph_file(self) -> Path:
        return graph_path(self.root)

    @property
    def index_file(self) -> Path:
        return index_path(self.root)

    def graph(self) -> Graph:
        path = self.graph_file
        if not path.exists():
            raise ToolError(
                f"no graph at {path}. Run `codegraph build` (or the rebuild_graph tool) first."
            )
        mtime = path.stat().st_mtime
        if self._graph is None or mtime != self._graph_mtime:
            self._graph = Graph.load(path)
            self._graph_mtime = mtime
        return self._graph

    def index(self) -> Index | None:
        path = self.index_file
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        if self._index is None or mtime != self._index_mtime:
            self._index = Index.load(path)
            self._index_mtime = mtime
        return self._index

    def config(self) -> Config:
        return Config.load_or_default(self.root)

    def annotations(self) -> AnnotationStore:
        return AnnotationStore(Config.dir_for(self.root))

    def persist(self, graph: Graph) -> None:
        """Save after a write tool, so the change is queryable immediately."""
        graph.save(self.graph_file)
        self._graph = graph
        self._graph_mtime = self.graph_file.stat().st_mtime


# --------------------------------------------------------------------------
#  Tools
# --------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = []
HANDLERS: dict[str, Callable[[Workspace, dict[str, Any]], Any]] = {}


def tool(name: str, description: str, schema: dict[str, Any]):
    def register(fn):
        TOOLS.append({
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "additionalProperties": False, **schema},
        })
        HANDLERS[name] = fn
        return fn
    return register


@tool(
    "graph_stats",
    "Overview of the code graph: node/edge counts, indexed roots, relation mix, "
    "and the most connected symbols. Call this first to see what is indexed.",
    {"properties": {}, "required": []},
)
def _graph_stats(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    stats = graph.stats()
    stats["hubs"] = [
        {"label": h["label"], "root": h["root"], "file": h["source_file"], "degree": h["degree"]}
        for h in graph.hubs(12)
    ]
    stats["graph_file"] = str(workspace.graph_file)
    age = time.time() - float(stats.get("built_at") or 0)
    stats["age_seconds"] = round(age, 1) if stats.get("built_at") else None
    return stats


@tool(
    "context_for_paths",
    "THE MAIN ENTRY POINT for 'what should I know before touching these files?'. "
    "Given file paths and/or symbol names, returns what is defined there, what "
    "depends on it, what it depends on, any human-asserted notes, and cross-root "
    "links. This is what the prompt hook calls automatically.",
    {
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "File paths, workspace-relative or absolute."},
            "symbols": {"type": "array", "items": {"type": "string"},
                        "description": "Symbol names (class, function, module)."},
            "prompt": {"type": "string",
                       "description": "Free text; paths and symbols are detected from it."},
            "budget": {"type": "integer", "description": "Approx token budget (default 1800)."},
        },
        "required": [],
    },
)
def _context_for_paths(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    paths = list(args.get("paths") or [])
    symbols = list(args.get("symbols") or [])
    prompt = args.get("prompt") or ""

    if prompt:
        index = workspace.index()
        if index is not None:
            found = detect(prompt, index)
            paths = list(dict.fromkeys(paths + found.files))
            symbols = list(dict.fromkeys(symbols + found.symbols))
    if not paths and not symbols:
        raise ToolError("give at least one of: paths, symbols, prompt")

    text = context_for(graph, files=paths, symbols=symbols, budget=int(args.get("budget", 1800)))
    return {
        "resolved_paths": paths,
        "resolved_symbols": symbols,
        "context": text or "(nothing in the graph matches those paths or symbols)",
    }


@tool(
    "find_symbol",
    "Search the graph for a symbol by name (exact first, then substring). "
    "Returns where each is defined and how connected it is.",
    {
        "properties": {
            "name": {"type": "string"},
            "limit": {"type": "integer", "description": "Max results (default 15)."},
        },
        "required": ["name"],
    },
)
def _find_symbol(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    name = _require(args, "name")
    found = graph.find_symbol(name, limit=int(args.get("limit", 15)))
    return {
        "query": name,
        "matches": [
            {
                "id": node_id,
                "label": graph.nodes[node_id].get("label"),
                "root": graph.nodes[node_id].get("root"),
                "file": graph.nodes[node_id].get("source_file"),
                "location": graph.nodes[node_id].get("source_location"),
                "degree": graph.degree(node_id),
            }
            for node_id in found
        ],
    }


@tool(
    "get_node",
    "Full detail for one node: its attributes and every edge touching it, "
    "with each edge's confidence and origin so parsed facts are distinguishable "
    "from inferred ones.",
    {
        "properties": {
            "id": {"type": "string", "description": "Node id, or a symbol name to resolve."},
            "limit": {"type": "integer", "description": "Max edges to return (default 40)."},
        },
        "required": ["id"],
    },
)
def _get_node(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    node_id = _resolve_node(graph, _require(args, "id"))
    limit = int(args.get("limit", 40))
    edges = []
    for other, edge, direction in graph.neighbours(node_id)[:limit]:
        target = graph.nodes.get(other, {})
        edges.append({
            "direction": direction,
            "relation": edge.get("relation"),
            "other": {"id": other, "label": target.get("label", other),
                      "file": target.get("source_file", "")},
            "confidence": edge.get("confidence", "EXTRACTED"),
            "origin": edge.get("_origin", "ast"),
            "evidence": edge.get("evidence", ""),
        })
    return {"node": graph.nodes[node_id], "degree": graph.degree(node_id), "edges": edges}


@tool(
    "impact_of",
    "Blast radius: what would be affected if this symbol or file changed. Walks "
    "dependency edges backwards (callers, importers, subclasses), breadth-first.",
    {
        "properties": {
            "target": {"type": "string", "description": "Symbol name, node id, or file path."},
            "depth": {"type": "integer", "description": "Hops to walk (default 2)."},
            "limit": {"type": "integer", "description": "Max results (default 40)."},
        },
        "required": ["target"],
    },
)
def _impact_of(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    target = _require(args, "target")
    depth = int(args.get("depth", 2))
    limit = int(args.get("limit", 40))

    starts = graph.nodes_in_file(target) or graph.find_symbol(target, limit=3)
    if target in graph.nodes:
        starts = [target]
    if not starts:
        raise ToolError(f"nothing in the graph matches {target!r}")

    seen: dict[str, dict[str, Any]] = {}
    for start in starts[:6]:
        for entry in graph.blast_radius(start, depth=depth, limit=limit):
            seen.setdefault(entry["id"], entry)
    ordered = sorted(seen.values(), key=lambda e: (e["distance"], -graph.degree(e["id"])))
    files = sorted({e["source_file"] for e in ordered if e["source_file"]})
    return {
        "target": target,
        "starts": starts[:6],
        "affected_count": len(ordered),
        "affected_files": files,
        "affected": ordered[:limit],
    }


@tool(
    "shortest_path",
    "How two things connect: the shortest chain of relationships between two "
    "symbols, files, or nodes. Works across roots via synthetic edges.",
    {
        "properties": {"source": {"type": "string"}, "target": {"type": "string"}},
        "required": ["source", "target"],
    },
)
def _shortest_path(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    source = _resolve_node(graph, _require(args, "source"))
    target = _resolve_node(graph, _require(args, "target"))
    path = graph.shortest_path(source, target)
    hops = []
    for left, right in pairwise(path):
        edge = next(
            (e for e in graph.outgoing(left) if e["target"] == right),
            next((e for e in graph.incoming(left) if e["source"] == right), {}),
        )
        hops.append({
            "from": graph.nodes[left].get("label", left),
            "to": graph.nodes[right].get("label", right),
            "relation": edge.get("relation", "?"),
            "confidence": edge.get("confidence", "?"),
            "origin": edge.get("_origin", "?"),
        })
    return {
        "source": source, "target": target,
        "found": bool(path), "length": max(0, len(path) - 1), "hops": hops,
    }


@tool(
    "list_roots",
    "The indexed codebases, their paths on disk, and how they are linked to each other.",
    {"properties": {}, "required": []},
)
def _list_roots(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    config = workspace.config()
    counts: dict[str, int] = {}
    for node in graph.nodes.values():
        counts[node.get("root", "?")] = counts.get(node.get("root", "?"), 0) + 1

    cross: dict[str, int] = {}
    for edge in graph.edges:
        source_root = graph.nodes.get(edge["source"], {}).get("root", "")
        target_root = graph.nodes.get(edge["target"], {}).get("root", "")
        if source_root and target_root and source_root != target_root:
            key = " <-> ".join(sorted((source_root, target_root)))
            cross[key] = cross.get(key, 0) + 1

    return {
        "roots": [
            {"name": r.name, "path": r.path, "nodes": counts.get(r.name, 0)}
            for r in config.roots
        ],
        "cross_root_edges": cross,
        "synthetic_rules": {
            "shared_symbol": config.synthetic.shared_symbol,
            "mirrored_path": config.synthetic.mirrored_path,
            "explicit": config.synthetic.explicit,
        },
    }


@tool(
    "add_to_graph",
    "Record a durable fact the parser cannot see - why code is the way it is, what "
    "a location really means, a constraint to respect. Attach it to a file, a "
    "'file.py:120' location, or a symbol. Survives every rebuild.",
    {
        "properties": {
            "target": {"type": "string",
                       "description": "File path, 'path/file.py:120', symbol name, or node id."},
            "note": {"type": "string", "description": "The fact to record."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "author": {"type": "string", "description": "Who asserted it (default 'agent')."},
        },
        "required": ["target", "note"],
    },
)
def _add_to_graph(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    store = workspace.annotations()
    annotation = Annotation(
        id=store.next_id(),
        kind="note",
        target=_require(args, "target"),
        text=_require(args, "note"),
        tags=list(args.get("tags") or []),
        author=args.get("author", "agent"),
        created_at=time.time(),
    )
    store.append(annotation)
    result = apply_annotations(graph, store.all())
    workspace.persist(graph)
    orphaned = [o for o in result["orphaned"] if o["id"] == annotation.id]
    return {
        "id": annotation.id,
        "attached": not orphaned,
        "warning": (
            f"{annotation.target!r} does not resolve to anything in the graph yet; "
            "the note is stored and will attach once it does."
        ) if orphaned else None,
        "total_annotations": result["applied"],
    }


@tool(
    "link_locations",
    "Assert a relationship between two locations, typically across roots where no "
    "import connects them (a client to its server, a spec to its implementation). "
    "Stored durably and re-applied on every rebuild.",
    {
        "properties": {
            "source": {"type": "string", "description": "File path, symbol, or node id."},
            "target": {"type": "string", "description": "File path, symbol, or node id."},
            "relation": {"type": "string",
                         "description": "e.g. implements, calls_over_http, generated_from."},
            "note": {"type": "string", "description": "Why they are linked."},
        },
        "required": ["source", "target"],
    },
)
def _link_locations(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    store = workspace.annotations()
    annotation = Annotation(
        id=store.next_id(),
        kind="link",
        target=_require(args, "source"),
        links_to=_require(args, "target"),
        relation=args.get("relation", "relates_to"),
        text=args.get("note", ""),
        author=args.get("author", "agent"),
        created_at=time.time(),
    )
    store.append(annotation)
    result = apply_annotations(graph, store.all())
    workspace.persist(graph)
    orphaned = [o for o in result["orphaned"] if o["id"] == annotation.id]
    return {
        "id": annotation.id,
        "linked": not orphaned,
        "relation": annotation.relation,
        "warning": "one or both endpoints did not resolve" if orphaned else None,
    }


@tool(
    "list_annotations",
    "Every human/agent-asserted fact in this workspace, with whether it still "
    "attaches to live code.",
    {"properties": {}, "required": []},
)
def _list_annotations(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    graph = workspace.graph()
    entries = workspace.annotations().all()
    result = apply_annotations(graph, entries)
    orphan_ids = {o["id"] for o in result["orphaned"]}
    return {
        "count": len(entries),
        "annotations": [
            {
                "id": a.id, "kind": a.kind, "target": a.target, "links_to": a.links_to,
                "relation": a.relation, "note": a.text, "tags": a.tags, "author": a.author,
                "attached": a.id not in orphan_ids,
            }
            for a in entries
        ],
    }


@tool(
    "revoke_annotation",
    "Retract a previously asserted fact by id.",
    {"properties": {"id": {"type": "string"}}, "required": ["id"]},
)
def _revoke_annotation(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    annotation_id = _require(args, "id")
    if not workspace.annotations().revoke(annotation_id):
        raise ToolError(f"no annotation with id {annotation_id!r}")
    graph = workspace.graph()
    apply_annotations(graph, workspace.annotations().all())
    workspace.persist(graph)
    return {"revoked": annotation_id}


@tool(
    "rebuild_graph",
    "Re-extract the graph from source. Deterministic and local (AST only, no model "
    "calls). Normally unnecessary - the edit hook keeps it current - but useful "
    "after a large external change such as a branch switch.",
    {
        "properties": {
            "incremental": {"type": "boolean",
                            "description": "Only re-parse changed files (default true)."},
            "roots": {"type": "array", "items": {"type": "string"},
                      "description": "Limit to these roots."},
        },
        "required": [],
    },
)
def _rebuild_graph(workspace: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    command = [sys.executable, "-m", "codegraph", "build", "--workspace", str(workspace.root)]
    if args.get("incremental", True):
        command.append("--incremental")
    for root in args.get("roots") or []:
        command += ["--root", str(root)]

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", "")])
    )
    completed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=900)
    if completed.returncode != 0:
        raise ToolError(
            "rebuild failed: "
            + (completed.stderr or completed.stdout or "no output").strip()[-600:]
        )
    return {"rebuilt": True, "output": completed.stdout.strip()[-1500:]}


# --------------------------------------------------------------------------
#  Helpers
# --------------------------------------------------------------------------

def _require(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None or str(value).strip() == "":
        raise ToolError(f"missing required argument {key!r}")
    return str(value)


def _resolve_node(graph: Graph, reference: str) -> str:
    if reference in graph.nodes:
        return reference
    in_file = graph.nodes_in_file(reference)
    if in_file:
        # The file's most connected node stands in for the file itself.
        return max(in_file, key=graph.degree)
    found = graph.find_symbol(reference, limit=1)
    if found:
        return found[0]
    raise ToolError(f"nothing in the graph matches {reference!r}")


# --------------------------------------------------------------------------
#  JSON-RPC plumbing
# --------------------------------------------------------------------------

class Server:
    def __init__(self, workspace: Workspace, stdout=None) -> None:
        self.workspace = workspace
        self.out = stdout or sys.stdout
        self.initialised = False

    def send(self, payload: dict[str, Any]) -> None:
        self.out.write(json.dumps(payload) + "\n")
        self.out.flush()

    def reply(self, request_id: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def fail(self, request_id: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        # Notifications carry no id and must never be answered.
        if request_id is None:
            return

        if method == "initialize":
            requested = str(params.get("protocolVersion", PROTOCOL_VERSION))
            self.initialised = True
            root_names = [root.name for root in self.workspace.config().roots] or ["(no roots)"]
            self.reply(request_id, {
                "protocolVersion": (
                    requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "codegraph", "version": __version__},
                "instructions": (
                    "Deterministic AST knowledge graph over "
                    f"{', '.join(root_names)}. "
                    "Call context_for_paths before editing files, impact_of before "
                    "changing a shared symbol, and add_to_graph to record decisions "
                    "the parser cannot see."
                ),
            })
        elif method == "ping":
            self.reply(request_id, {})
        elif method == "tools/list":
            self.reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            self._call_tool(request_id, params)
        elif method in ("resources/list", "prompts/list"):
            # Declared unsupported in capabilities, but some clients ask anyway.
            self.reply(request_id, {"resources": [], "prompts": []})
        else:
            self.fail(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(str(name))
        if handler is None:
            self.fail(request_id, INVALID_PARAMS, f"unknown tool: {name}")
            return
        try:
            result = handler(self.workspace, arguments)
            payload = json.dumps(result, indent=1, default=str)
            self.reply(request_id, {"content": [{"type": "text", "text": payload}]})
        except ToolError as exc:
            # A tool-level failure is data for the model, not a protocol error:
            # it is reported as a successful call with isError set, so the model
            # can read the message and try something else.
            self.reply(request_id, {
                "content": [{"type": "text", "text": f"error: {exc}"}],
                "isError": True,
            })
        except Exception as exc:
            print(traceback.format_exc(), file=sys.stderr)
            self.reply(request_id, {
                "content": [{"type": "text", "text": f"internal error in {name}: {exc}"}],
                "isError": True,
            })

    def serve_forever(self, stream=None) -> int:
        stream = stream or sys.stdin
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None,
                           "error": {"code": PARSE_ERROR, "message": "invalid JSON"}})
                continue
            if isinstance(message, list):
                for item in message:
                    self.handle(item)
            elif isinstance(message, dict):
                self.handle(message)
            else:
                self.send({"jsonrpc": "2.0", "id": None,
                           "error": {"code": INVALID_REQUEST, "message": "expected an object"}})
        return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="codegraph-serve", description=__doc__)
    parser.add_argument("--workspace", default=None, help="workspace root (default: discovered)")
    args = parser.parse_args(argv)

    root = Path(args.workspace).resolve() if args.workspace else find_workspace()
    return Server(Workspace(root)).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
