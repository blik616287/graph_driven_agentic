# graph_driven_agentic

Two related pieces of work: a **code knowledge graph for coding agents**, and a
**codebase worth pointing it at**.

| | what it is | size |
|---|---|---|
| [`codegraph/`](codegraph/) | Deterministic multi-root code knowledge graph, served over MCP, with hooks that inject structural context on every prompt and rebuild the graph after every edit. Packaged as a Claude Code plugin and an APM package. | ~5,700 lines, 148 tests |
| [`example_src/`](example_src/) | **marketd** — a miniature trading venue written to be read: HTTP/1.1 server on asyncio, price-time-priority matching engine, double-entry ledger, client SDK. Dependency-free. | ~4,800 lines, 123 tests |

They are independent — either stands alone — but they were built together, and
each is better for it. `example_src` gave `codegraph` a dense, real corpus to be
correct against instead of a toy fixture; `codegraph` gave `example_src` a reason
to have the kind of structure worth graphing.

---

## Quick start

Index the example service and wire the graph into your coding assistant:

```bash
cd codegraph
make install                    # venv + graphify backend (~15s, no API key)
make init ROOT=../example_src   # index it, build the graph
make install-claude             # MCP server + both hooks + CLAUDE.md
```

From then on, a prompt that mentions indexed code gets a structural briefing
before the model sees it, and every `Write`/`Edit` refreshes the graph.

**After cloning**, the roots are already in `.codegraph/config.json`, so skip
`init` and just build:

```bash
cd codegraph
make install          # the backend venv is not committed
make build            # rebuild the graph from the committed config
make install-claude   # regenerate the assistant wiring for your machine
```

The graph and the generated assistant config (`.mcp.json`,
`.claude/settings.json`) are derived and machine-specific — they bake in
absolute paths — so they are not in version control. The plugin itself and
`.codegraph/config.json` are, which is what lets a clone rebuild to the same
graph.

Run the example service on its own:

```bash
cd example_src
python demo.py              # boots a server, drives it end to end
python bench.py             # hot-path microbenchmarks
python -m pytest tests -q   # 123 tests
```

## What codegraph does

- **Deterministic.** Tree-sitter AST parsing only — no embeddings, no model
  calls, no network on the build path. The same commit always produces the same
  graph, which is what makes it safe to rebuild from a hook on every edit.
- **Multi-root.** Index a server and its client together. Nothing imports across
  them, so codegraph infers *synthetic* edges — shared symbols, mirrored paths —
  and lets you assert your own. Every inferred edge is tagged, so an agent can
  tell a parsed fact from a guess.
- **Extensible by hand.** "Add this to the graph" records a constraint or a
  reason the parser cannot derive, in an append-only log that survives every
  rebuild.
- **Two hooks close the loop.** `UserPromptSubmit` injects context (~1 ms when
  the prompt mentions nothing indexed); `PostToolUse` re-parses the edited root
  (~0.2 s, debounced and lock-guarded).

Backends are pluggable: [graphify](https://github.com/Graphify-Labs/graphify)
(default) or [code-review-graph](https://github.com/tirth8205/code-review-graph).
Neither is vendored here — `make install` fetches them from PyPI.

See [`codegraph/README.md`](codegraph/README.md) for the full reference.

## What example_src teaches

`marketd` exists to be read. The domain is complex enough to need real structure
— layers, caches, invariants, hot paths — and small enough to hold in your head.

Functions that run once per request, order, or fill are marked `HOT PATH` in the
source, and `bench.py` prices each one. The ordering is the lesson: HTTP header
parsing and schema validation each cost more than the entire order-book
operation they guard.

It also documents its own invariants — the books balance, funds cannot be
double-spent, fills happen at the maker's price — and asserts them in tests.

See [`example_src/README.md`](example_src/README.md).

## Layout

```
codegraph/                 the plugin
  Makefile                 install, build, wire up, test
  src/codegraph/           model, build pipeline, synthetic edges, MCP server
  scripts/                 CLI + the two hooks
  hooks/ commands/ skills/ Claude Code plugin components
  apm/apm.yml              APM package manifest
  examples/marketd-client/ demo fixture: a second, related root
  tests/                   148 tests

example_src/               marketd, the example service
  marketd/                 http, api, services, core, storage, workers, telemetry
  demo.py bench.py         walkthrough and microbenchmarks
  tests/                   123 tests

.codegraph/                the built graph for this repo
  config.json              roots and rules        -> in git
  annotations.jsonl        asserted facts         -> in git
  graph.json index.json    derived                -> gitignored
```

## Requirements

Python 3.11+ and `make`. Nothing else — `example_src` is standard library only,
and codegraph's MCP server and hooks are too. Only the graph *builder* pulls in
a backend, into a local virtualenv.

## Licence

MIT — Martin Forde `<mforde84@gmail.com>` (bliklabs). See [`LICENSE`](LICENSE).

The graph backends are installed from PyPI rather than vendored; their licences
(graphify: Apache-2.0, code-review-graph: MIT) are reproduced in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
