#!/usr/bin/env python3
"""End-to-end walkthrough of codegraph on a throwaway workspace.

Builds a real graph over two related codebases, shows what the prompt hook would
inject, records a fact, and proves it survives a rebuild. Everything runs in a
temporary directory and is cleaned up.

    make demo
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parent
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def rule(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}\n" + "-" * max(len(title), 62))


def main() -> int:
    from codegraph.annotate import Annotation, AnnotationStore
    from codegraph.build import build
    from codegraph.config import Config, Root
    from codegraph.context import Index, context_for, detect
    from codegraph.model import Graph

    repo_root = PACKAGE_ROOT.parent
    marketd = repo_root / "example_src"
    client = PACKAGE_ROOT / "examples" / "marketd-client"
    if not marketd.is_dir():
        print(f"expected the example service at {marketd}", file=sys.stderr)
        return 1

    workspace = Path(tempfile.mkdtemp(prefix="codegraph-demo-"))
    print(f"{BOLD}codegraph demo{RESET}  workspace {workspace}")
    backend_python = os.environ.get("CODEGRAPH_BACKEND_PYTHON") or sys.executable

    try:
        # ---------------------------------------------------------- 1. build
        rule("1. Index two related codebases")
        config = Config(
            roots=[
                Root(name="marketd", path=str(marketd)),
                Root(name="client", path=str(client)),
            ]
        )
        config.save(workspace)
        report = build(workspace, config, backend_python=backend_python)
        print(f"  {report.summary()}")
        for name, info in report.roots.items():
            print(f"    {name:<10} {info.get('nodes', 0):>5} nodes  {info.get('path', '')}")
        print(f"  {DIM}AST only - no API key, no network, no model calls{RESET}")

        graph = Graph.load(workspace / ".codegraph" / "graph.json")

        # ------------------------------------------------------ 2. synthetic
        rule("2. Synthetic edges link roots that share no imports")
        synthetic = [e for e in graph.edges if e.get("_origin") == "codegraph-synthetic"]
        if not synthetic:
            print("  (none inferred - the roots share no distinctive symbols)")
        for edge in synthetic:
            left, right = graph.nodes[edge["source"]], graph.nodes[edge["target"]]
            print(f"  {left.get('label'):<14}[{left.get('root')}] --{edge['relation']}--> "
                  f"{right.get('label'):<14}[{right.get('root')}]   {DIM}{edge.get('rule')}{RESET}")
        print(f"  {DIM}tagged SYNTHETIC, never presented as parsed fact{RESET}")

        # -------------------------------------------------------- 3. the hook
        rule("3. What the prompt hook injects")
        index = Index.load(workspace / ".codegraph" / "index.json")
        for prompt in (
            "fix the cancel bug in example_src/marketd/core/book.py",
            "what is the weather today",
        ):
            elapsed = time.perf_counter()
            found = detect(prompt, index)
            micros = (time.perf_counter() - elapsed) * 1000
            print(f"\n  prompt: {prompt!r}")
            print(f"  detected in {micros:.2f}ms -> files={found.files} symbols={found.symbols}")
            if not found:
                print(f"  {DIM}nothing indexed is mentioned; the hook stays silent{RESET}")
                continue
            context = context_for(graph, files=found.files, symbols=found.symbols, budget=700)
            for line in context.splitlines()[2:]:
                print(f"    {line}")

        # -------------------------------------------------- 4. blast radius
        rule("4. Blast radius before a change")
        target = graph.find_symbol("OrderBook")[0]
        affected = graph.blast_radius(target, depth=2, limit=40)
        files = sorted({entry["source_file"] for entry in affected if entry["source_file"]})
        print(f"  changing OrderBook could affect {len(affected)} symbols in {len(files)} files:")
        for path in files:
            print(f"    {path}")

        # --------------------------------------------------- 5. interactive
        rule("5. Record a fact the parser cannot see")
        store = AnnotationStore(workspace / ".codegraph")
        note = (
            "iter_levels() consumes the heap as it walks. Never call it from a read "
            "path - use walk(), which sorts a copy."
        )
        annotation = Annotation(
            id=store.next_id(), kind="note",
            target=f"{marketd}/marketd/core/book.py:195",
            text=note, author="demo", created_at=time.time(),
        )
        store.append(annotation)
        print(f"  recorded {annotation.id} on book.py:195")
        print(f"    {DIM}{note}{RESET}")

        rule("6. It survives a rebuild")
        report = build(workspace, config, incremental=True, backend_python=backend_python)
        graph = Graph.load(workspace / ".codegraph" / "graph.json")
        notes = [n for n in graph.nodes.values() if n.get("_origin") == "codegraph-annotation"]
        print(f"  rebuilt: {report.summary()}")
        print(f"  annotations still attached: {len(notes)}")
        for node in notes:
            attached = [
                graph.nodes[edge["target"]].get("label")
                for edge in graph.outgoing(node["id"])
            ]
            print(f"    ! {node.get('label')}")
            print(f"      attached to: {', '.join(str(a) for a in attached[:4])}")
        print(f"  {DIM}the graph is derived and disposable; the annotation log is not{RESET}")

        rule("Done")
        print("  the same graph is served over MCP:  make serve")
        print("  wire it into Claude Code:            make install-claude")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
