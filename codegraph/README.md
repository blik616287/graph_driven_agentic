# codegraph

A deterministic, multi-root code knowledge graph for coding agents — served over
MCP, kept current by hooks, and extensible by hand.

```bash
make install                    # venv + graphify backend  (~15s, no API key)
make init ROOT=../example_src   # index a codebase and build the graph
make install-claude             # wire the MCP server + both hooks into Claude Code
```

That is the whole setup. From then on the agent gets structural context on every
prompt that touches indexed code, and the graph rebuilds itself after every edit.

---

## What it does

**Builds a graph, deterministically.** Tree-sitter AST parsing only. No
embeddings, no model calls, no network on the build path. The same commit always
produces the same graph, which is what makes it safe to rebuild from a hook that
fires on every file edit.

**Serves it over MCP.** Twelve tools, the important one being
`context_for_paths`: *given these files, what depends on them, what do they
depend on, and what has anyone recorded about them?*

**Links multiple codebases.** Index a server and its client together. Nothing
imports across them, so codegraph infers **synthetic edges** — shared symbol
names, mirrored paths — and lets you assert them by hand. Every inferred edge is
tagged, so an agent can always tell a parsed fact from a guess.

**Accepts new knowledge.** "Add this to the graph" records a fact the parser
cannot derive — a constraint, a reason, a trap — attached to the code it is
about, in an append-only log that survives every rebuild.

**Two hooks close the loop:**

| hook | when | what it does |
|---|---|---|
| `UserPromptSubmit` | every prompt | detects references to indexed code and injects a compact briefing. ~1 ms when the prompt mentions nothing. |
| `PostToolUse` | after Write/Edit | incrementally re-parses the edited root. Debounced and lock-guarded, so a burst of edits triggers one rebuild. |

---

## Install

Requires Python 3.10+ and `make`. Nothing else.

```bash
cd codegraph
make install          # graphify backend (Apache-2.0) — the default
make install-crg      # code-review-graph backend (MIT) — optional
make install-all      # both
make doctor           # check interpreters, backends, roots, graph age
```

Backends go in a local `.venv`. **codegraph itself, the MCP server, and both
hooks are standard-library only** — they run under whatever Python the assistant
launches them with, and keep working if the venv is missing or broken.

Switch backends per workspace with `"backend": "code-review-graph"` in
`.codegraph/config.json`.

> Behind a private package index? `make install` warns and falls back to PyPI.
> If your proxy blocks that too, install `graphifyy` however you normally would
> and point at it: `make build BACKEND_PYTHON=/path/to/python`.

## Index some code

```bash
make init ROOT=../example_src              # first root; builds the graph
make add-root ROOT=../services/api         # more roots, linked synthetically
make add-root ROOT=~/work/other-repo NAME=other

make build                                 # full deterministic rebuild
make incremental                           # re-parse only changed files
make status
```

Roots may live anywhere on disk; they do not have to be inside the workspace.

```
.codegraph/
  config.json        roots, backend, synthetic rules      -> commit this
  annotations.jsonl  facts you asserted                   -> commit this
  graph.json         the merged graph                     -> derived, gitignored
  index.json         small index the prompt hook reads    -> derived
  roots/<name>.json  per-root graphs + backend AST cache  -> derived
```

The split matters: everything derived is rebuildable in under a second.
`annotations.jsonl` is not — it holds the only facts in the graph that no parser
could recover.

## Wire it into an assistant

```bash
make install-claude     # .mcp.json + .claude/settings.json hooks + CLAUDE.md
make install-cursor     # .cursor/mcp.json + rule
make install-codex      # .mcp.json + AGENTS.md
make install-vscode     # .vscode/mcp.json + copilot-instructions.md
make install-gemini     # .gemini/settings.json + GEMINI.md
```

Existing config is merged, never clobbered; Markdown edits land inside a
`<!-- codegraph:begin -->` block. Reinstalling is byte-identical.

### As a Claude Code plugin

This directory *is* a plugin (`.claude-plugin/plugin.json`), bundling the MCP
server, both hooks, a `/codegraph` command and a skill:

```bash
claude plugin validate .                    # passes
/plugin marketplace add /path/to/this/repo/codegraph
/plugin install codegraph@bliklabs
```

Or drop it in place: `ln -s "$PWD" ~/.claude/skills/codegraph`.

### As an APM package

`apm/apm.yml` declares the same primitives for
[microsoft/apm](https://github.com/microsoft/apm):

```bash
make apm-pack     # print the manifest
apm install
```

---

## The MCP tools

**Read**

| tool | question it answers |
|---|---|
| `context_for_paths` | what should I know before touching these files? |
| `impact_of` | what breaks if I change this? |
| `shortest_path` | how do these two things connect? |
| `find_symbol` / `get_node` | where is this defined, and what touches it? |
| `graph_stats` / `list_roots` | what is indexed? |

**Write**

| tool | what it records |
|---|---|
| `add_to_graph` | a durable fact about a location |
| `link_locations` | a relationship across repositories |
| `list_annotations` / `revoke_annotation` | manage what has been asserted |
| `rebuild_graph` | force a refresh |

Debug the server directly:

```bash
make serve      # stdio JSON-RPC
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | make serve
```

---

## Multiple roots and synthetic edges

Two codebases that share no imports still relate to each other. codegraph infers
those links conservatively and labels every one:

```
Order       [client] --mirrors--> Order       [marketd]   rule=shared_symbol
Instrument  [client] --mirrors--> Instrument  [marketd]   rule=shared_symbol
```

| rule | infers | guards against noise by |
|---|---|---|
| `shared_symbol` | same distinctive symbol in two roots | skipping short names, stopwords (`main`, `config`), dunders, bare method names, filenames, and any name defined more than once inside a root |
| `mirrored_path` | same root-relative file path | stripping each root's own prefix first; ignoring `__init__.py` and friends |
| `explicit` | whatever you declare | nothing — you asserted it |

**A wrong edge is worse than a missing one**, because an agent will reason from
it. Pointed at this repo's own `codegraph/src` and `example_src`, the rules infer
*nothing* — the two share only generic Python vocabulary, and saying so is the
correct answer. Point it at `examples/marketd-client` and it finds exactly the
four domain types the client really does mirror.

Declare a link yourself when the heuristics cannot see it:

```bash
./scripts/codegraph link \
    codegraph/examples/marketd-client/marketdclient/trading.py \
    example_src/marketd/api/handlers/orders.py \
    --relation calls_over_http --note "the client's only wire boundary"
```

## Adding knowledge by hand

```bash
./scripts/codegraph add "example_src/marketd/core/book.py:195" \
  "iter_levels() consumes the heap as it walks. Never call it from a read path."
```

Or, in a session: *"add to the graph that book.py:195 consumes the heap."* The
agent calls `add_to_graph`.

A `path:line` target resolves to the **enclosing definition** — a note on line
195 attaches to the function that starts at 195, not to the file. When code
moves and a target stops resolving, the annotation is reported as *orphaned*
rather than deleted, and reattaches by itself if the target comes back.

## Edge provenance

Every edge says where it came from. This is the single most important thing to
understand about reading codegraph output:

| `_origin` | `confidence` | meaning | trust |
|---|---|---|---|
| `ast` | `EXTRACTED` | parsed from source | fact |
| `ast` | `INFERRED` | resolved by the backend | strong |
| `codegraph-synthetic` | `SYNTHETIC` | inferred across roots by a heuristic | a lead |
| `codegraph-synthetic` | `ASSERTED` | declared in config | as good as its author |
| `codegraph-annotation` | `ASSERTED` | recorded by a person or agent | trust, but date-check |

The injected context marks them: `~` for inferred, `!` for asserted.

---

## How it performs

On this repository (1,380 nodes across three roots, on CPython 3.13):

| operation | time |
|---|---|
| prompt hook, no code mentioned | ~1 ms *(the common case — index only)* |
| prompt hook, context injected | ~17 ms |
| incremental rebuild after one edit | ~0.2 s |
| full rebuild, 3 roots | ~1.0 s |
| MCP tool call | < 10 ms |

The prompt hook's fast path is the design point. It runs before *every* prompt,
so it answers "does this mention anything indexed?" from a 36 KB index and exits
without ever loading the graph. Only a hit pays for the rest.

## Failure behaviour

Both hooks **always exit 0**. A hook that errors breaks the user's session,
which is a far worse outcome than missing context:

- no graph, no workspace, corrupt index, unreadable payload → exit silently
- a backend failure during rebuild → keep the last good graph per root and
  report the error, rather than serving nothing
- concurrent rebuilds → a pid-stamped lock, stolen if its owner is gone, so a
  killed build cannot wedge the graph
- graph rewritten mid-session → the MCP server reloads on mtime change

Set `CODEGRAPH_HOOK_DEBUG=1` to see what a hook decided and why.

## Development

```bash
make test     # 146 tests, ~1s
make demo     # end-to-end walkthrough on the bundled example
make clean    # drop derived data (config and annotations survive)
```

Tests run against a fake backend so they need no tree-sitter install; the one
that exercises a real backend is marked `integration` and skips without one.

```
codegraph/
  Makefile                  install, build, wire up, test
  src/codegraph/
    model.py                graph: namespace, merge, traverse   (no networkx)
    build.py                the pipeline
    synthetic.py            cross-root edge inference
    annotate.py             durable asserted facts
    context.py              prompt detection + context assembly
    server.py               MCP server                          (stdlib JSON-RPC)
    install.py              per-assistant wiring
    backends/               graphify | code-review-graph adapters
  scripts/
    codegraph               CLI launcher
    codegraph-prompt-hook   UserPromptSubmit
    codegraph-edit-hook     PostToolUse
  hooks/ commands/ skills/  Claude Code plugin components
  apm/apm.yml               APM package manifest
  examples/marketd-client/  demo fixture: a second, related root
  tests/                    146 tests
```

## Licensing

codegraph is MIT (see `../LICENSE`). The extraction backends are **not** vendored
here — `make install` fetches them from PyPI, and their licenses (graphify:
Apache-2.0; code-review-graph: MIT) are reproduced in
`../THIRD_PARTY_NOTICES.md`.
