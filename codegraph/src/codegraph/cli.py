"""Command line interface.

    codegraph init [PATH...]        create a workspace, optionally with roots
    codegraph add-root PATH         index another codebase
    codegraph build                 (re)build the graph
    codegraph status                what is indexed, and how stale
    codegraph context PATH|SYMBOL   what the prompt hook would inject
    codegraph add TARGET NOTE       record a durable fact ("add to graph")
    codegraph link A B --relation R assert a cross-root relationship
    codegraph annotations           list / revoke asserted facts
    codegraph serve                 run the MCP server on stdio
    codegraph doctor                check backends and workspace health
    codegraph install               wire into a coding assistant
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .annotate import Annotation, AnnotationStore
from .annotate import apply as apply_annotations
from .backends import available_backends
from .build import build, index_path, load_state
from .config import Config, Root, find_workspace
from .context import Index, context_for, detect
from .model import Graph

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def _workspace(args) -> Path:
    return Path(args.workspace).resolve() if args.workspace else find_workspace()


def _emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))


def cmd_init(args) -> int:
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    config = Config.load_or_default(workspace)
    for raw in args.paths or []:
        path = Path(raw)
        name = args.name or _slug(path)
        resolved = path.resolve()
        relative = resolved.relative_to(workspace) if _under(resolved, workspace) else resolved
        try:
            config.add_root(Root(name=name, path=str(relative)))
        except ValueError as exc:
            print(f"skipping: {exc}", file=sys.stderr)
    if args.backend:
        config.backend = args.backend
    saved = config.save(workspace)
    _ensure_gitignore(workspace)
    print(f"{BOLD}codegraph workspace{RESET} {workspace}")
    print(f"  config  {saved}")
    print(f"  backend {config.backend}")
    names = [root.name for root in config.roots] or "(none yet - use `codegraph add-root PATH`)"
    print(f"  roots   {names}")
    if config.roots:
        print("\nnext: codegraph build")
    return 0


def cmd_add_root(args) -> int:
    workspace = _workspace(args)
    config = Config.load(workspace)
    path = Path(args.path).resolve()
    if not path.is_dir():
        print(f"not a directory: {path}", file=sys.stderr)
        return 1
    relative = path.relative_to(workspace) if _under(path, workspace) else path
    name = args.name or _slug(path)
    try:
        config.add_root(Root(name=name, path=str(relative)))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    config.save(workspace)
    print(f"added root {BOLD}{name}{RESET} -> {relative}")
    print(f"roots now: {[r.name for r in config.roots]}")
    print("\nnext: codegraph build   (synthetic edges are inferred between roots automatically)")
    return 0


def cmd_build(args) -> int:
    workspace = _workspace(args)
    config = Config.load(workspace)
    report = build(
        workspace,
        config,
        roots=args.root or None,
        incremental=args.incremental,
        backend_python=args.backend_python,
    )
    if args.json:
        _emit(report.to_dict(), True)
    else:
        print(f"{BOLD}built{RESET} {report.summary()}")
        print(f"  graph {report.graph_path}")
        for name, info in report.roots.items():
            flags = " (stale)" if info.get("stale") else " (reused)" if info.get("reused") else ""
            print(f"  root {name}: {info.get('nodes', 0)} nodes{flags}")
        if any(report.synthetic.values()):
            print(f"  synthetic edges: {report.synthetic}")
        if report.annotations.get("orphaned"):
            print(f"  {DIM}orphaned annotations (target moved or gone):{RESET}")
            for orphan in report.annotations["orphaned"][:5]:
                print(f"    {orphan['id']} -> {orphan['target']}")
        for error in report.errors:
            print(f"  {BOLD}error{RESET} {error}", file=sys.stderr)
    return 1 if report.errors and not report.nodes else 0


def cmd_status(args) -> int:
    workspace = _workspace(args)
    config = Config.load_or_default(workspace)
    state = load_state(workspace)
    graph_file = Config.dir_for(workspace) / "graph.json"

    payload = {
        "workspace": str(workspace),
        "backend": config.backend,
        "roots": [{"name": r.name, "path": r.path} for r in config.roots],
        "graph_exists": graph_file.exists(),
        "last_build": state.get("last_build"),
        "nodes": state.get("nodes"),
        "edges": state.get("edges"),
    }
    if graph_file.exists():
        graph = Graph.load(graph_file)
        payload.update(graph.stats())
        payload["age_seconds"] = round(time.time() - graph_file.stat().st_mtime, 1)
    if args.json:
        _emit(payload, True)
        return 0

    print(f"{BOLD}codegraph{RESET} {__version__}  workspace {workspace}")
    print(f"  backend {config.backend}")
    if not graph_file.exists():
        print("  graph   (not built) - run `codegraph build`")
        return 0
    print(f"  graph   {payload['nodes']} nodes, {payload['edges']} edges, "
          f"{payload.get('files', 0)} files, {payload['age_seconds']}s old")
    root_counts = payload.get("roots") or {}
    for name, count in (root_counts.items() if isinstance(root_counts, dict) else []):
        print(f"    root {name}: {count} nodes")
    origins = payload.get("edge_origins", {})
    if origins:
        print(f"  edges by origin: {origins}")
    return 0


def cmd_context(args) -> int:
    workspace = _workspace(args)
    graph = Graph.load(Config.dir_for(workspace) / "graph.json")
    files, symbols = [], []
    if args.prompt:
        index = Index.load(index_path(workspace))
        found = detect(args.prompt, index)
        files, symbols = found.files, found.symbols
        if args.json:
            _emit({"detected": found.to_dict()}, True)
    for target in args.targets or []:
        (files if ("/" in target or "." in target) else symbols).append(target)
    text = context_for(graph, files=files, symbols=symbols, budget=args.budget)
    print(text or "(nothing in the graph matches)")
    return 0


def cmd_add(args) -> int:
    workspace = _workspace(args)
    store = AnnotationStore(Config.dir_for(workspace))
    annotation = Annotation(
        id=store.next_id(), kind="note", target=args.target,
        text=args.note, tags=args.tag or [], author=args.author, created_at=time.time(),
    )
    store.append(annotation)
    graph_file = Config.dir_for(workspace) / "graph.json"
    attached = None
    if graph_file.exists():
        graph = Graph.load(graph_file)
        result = apply_annotations(graph, store.all())
        graph.save(graph_file)
        attached = not any(o["id"] == annotation.id for o in result["orphaned"])
    print(f"{BOLD}recorded{RESET} {annotation.id} on {args.target}")
    if attached is False:
        print(f"  {DIM}note: target does not resolve yet; stored and will attach on rebuild{RESET}")
    return 0


def cmd_link(args) -> int:
    workspace = _workspace(args)
    store = AnnotationStore(Config.dir_for(workspace))
    annotation = Annotation(
        id=store.next_id(), kind="link", target=args.source, links_to=args.target,
        relation=args.relation, text=args.note or "", author=args.author, created_at=time.time(),
    )
    store.append(annotation)
    graph_file = Config.dir_for(workspace) / "graph.json"
    if graph_file.exists():
        graph = Graph.load(graph_file)
        apply_annotations(graph, store.all())
        graph.save(graph_file)
    print(f"{BOLD}linked{RESET} {args.source} --{args.relation}--> {args.target}")
    print(f"  {annotation.id}")
    return 0


def cmd_annotations(args) -> int:
    workspace = _workspace(args)
    store = AnnotationStore(Config.dir_for(workspace))
    if args.revoke:
        ok = store.revoke(args.revoke)
        message = f"revoked {args.revoke}" if ok else f"no annotation {args.revoke}"
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    entries = store.all()
    if args.json:
        _emit([e.__getstate__() if hasattr(e, "__getstate__") else vars(e) for e in entries], True)
        return 0
    if not entries:
        print("no annotations yet - record one with `codegraph add <target> \"<note>\"`")
        return 0
    for entry in entries:
        head = f"{entry.id}  [{entry.kind}]  {entry.target}"
        if entry.kind == "link":
            head += f" --{entry.relation}--> {entry.links_to}"
        print(f"{BOLD}{head}{RESET}")
        if entry.text:
            print(f"    {entry.text}")
    return 0


def cmd_serve(args) -> int:
    from .server import Server, Workspace

    return Server(Workspace(_workspace(args))).serve_forever()


def cmd_doctor(args) -> int:
    workspace = _workspace(args)
    print(f"{BOLD}codegraph {__version__}{RESET}")
    print(f"  python     {sys.version.split()[0]} ({sys.executable})")
    print(f"  workspace  {workspace}")

    config_file = Config.path_for(workspace)
    config_state = f"ok {config_file}" if config_file.exists() else "MISSING - run `codegraph init`"
    print(f"  config     {config_state}")

    backend_python = args.backend_python or os.environ.get("CODEGRAPH_BACKEND_PYTHON")
    print(f"  backend py {backend_python or sys.executable}")
    print(f"\n{BOLD}backends{RESET}")
    ok = False
    for name, status in available_backends(python=backend_python).items():
        marker = "ok " if not status.startswith("error") else "-- "
        ok = ok or not status.startswith("error")
        print(f"  {marker}{name}: {status}")
    if not ok:
        print(f"\n  {DIM}no backend available - run `make install`{RESET}")

    if config_file.exists():
        config = Config.load(workspace)
        print(f"\n{BOLD}roots{RESET}")
        for root in config.roots:
            resolved = root.resolve(workspace)
            print(f"  {'ok ' if resolved.is_dir() else '-- '}{root.name}: {resolved}")
        graph_file = Config.dir_for(workspace) / "graph.json"
        if graph_file.exists():
            stats = Graph.load(graph_file).stats()
            age = time.time() - graph_file.stat().st_mtime
            print(f"\n{BOLD}graph{RESET}")
            print(f"  {stats['nodes']} nodes, {stats['edges']} edges, {age:.0f}s old")
        else:
            print(f"\n{BOLD}graph{RESET}\n  not built - run `codegraph build`")
    return 0 if ok else 1


def cmd_install(args) -> int:
    from .install import PLATFORMS, install_platform

    workspace = _workspace(args)
    targets = args.platform or ["claude"]
    for platform in targets:
        if platform not in PLATFORMS:
            print(f"unknown platform {platform!r}; known: {sorted(PLATFORMS)}", file=sys.stderr)
            return 1
        for line in install_platform(platform, workspace, scope=args.scope):
            print(line)
    return 0


def _slug(path: Path) -> str:
    name = path.resolve().name or "root"
    cleaned = "".join(char if (char.isalnum() or char in "-_") else "-" for char in name)
    return cleaned.strip("-").lower()


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _ensure_gitignore(workspace: Path) -> None:
    """Ignore derived artifacts but keep config and annotations in git.

    The graph is rebuildable; the annotation log is not - it holds facts nobody
    can regenerate from source.
    """
    target = Config.dir_for(workspace) / ".gitignore"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Derived - rebuilt by `codegraph build`.\n"
        "graph.json\ngraph.json.tmp\nindex.json\nindex.json.tmp\nstate.json\nroots/\n"
        "\n# Kept in version control: config.json and annotations.jsonl hold facts\n"
        "# that cannot be regenerated from source.\n!config.json\n!annotations.jsonl\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codegraph", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"codegraph {__version__}")
    parser.add_argument("--workspace", default=None, help="workspace root (default: auto-discover)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="create a workspace")
    p.add_argument("paths", nargs="*", help="codebases to index")
    p.add_argument("--name", help="root name (only with a single path)")
    p.add_argument("--backend", choices=["graphify", "code-review-graph"])
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add-root", help="index another codebase")
    p.add_argument("path")
    p.add_argument("--name")
    p.set_defaults(func=cmd_add_root)

    p = sub.add_parser("build", help="(re)build the graph")
    p.add_argument("--root", action="append", help="limit to this root (repeatable)")
    p.add_argument("--incremental", action="store_true", help="only re-parse changed files")
    p.add_argument("--backend-python", default=os.environ.get("CODEGRAPH_BACKEND_PYTHON"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("status", help="what is indexed")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("context", help="show the context the prompt hook would inject")
    p.add_argument("targets", nargs="*", help="file paths or symbol names")
    p.add_argument("--prompt", help="detect targets from this text instead")
    p.add_argument("--budget", type=int, default=1800)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("add", help="record a durable fact about a location")
    p.add_argument("target", help="file path, 'file.py:120', or symbol")
    p.add_argument("note")
    p.add_argument("--tag", action="append")
    p.add_argument("--author", default="human")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("link", help="assert a relationship between two locations")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--relation", default="relates_to")
    p.add_argument("--note")
    p.add_argument("--author", default="human")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("annotations", help="list or revoke asserted facts")
    p.add_argument("--revoke", metavar="ID")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_annotations)

    p = sub.add_parser("serve", help="run the MCP server on stdio")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("doctor", help="check backends and workspace health")
    p.add_argument("--backend-python", default=None)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("install", help="wire codegraph into a coding assistant")
    p.add_argument("--platform", action="append",
                   help="claude | cursor | codex | agents | vscode (repeatable)")
    p.add_argument("--scope", default="project", choices=["project", "user"])
    p.set_defaults(func=cmd_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
