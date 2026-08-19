---
name: codegraph-context
description: Use the code knowledge graph to orient before editing, to find the blast radius of a change, and to record durable facts about the codebase. Trigger when the user asks what depends on something, how two parts connect, what a change would break, when they say "add this to the graph" / "remember this about <file>", or before making a non-trivial edit to unfamiliar code.
---

# Working with codegraph

A deterministic AST knowledge graph of this project's codebases is available
over MCP. It is built by tree-sitter parsing only, and a hook rebuilds it after
every edit, so it describes the code as it is now.

## Use it before reading files

Reading a file tells you what it contains. The graph tells you what *depends on
it*, which is the part that makes a change safe or unsafe and the part you
cannot get by reading the file itself.

Before a non-trivial edit:

1. `context_for_paths` with the paths you are about to touch.
2. If you are changing a shared symbol, `impact_of` it.
3. Only then open the files.

This is not about saving tokens. It is that "who calls this" is answerable in
one query and unanswerable by reading.

## Reading tool output honestly

Every edge carries an origin, and they are not equally trustworthy:

| origin | meaning | how to treat it |
|---|---|---|
| `ast` | parsed from source | fact |
| `codegraph-synthetic` | inferred across roots by a heuristic | a lead; verify before relying on it |
| `codegraph-annotation` | asserted by a person or an agent | trust the claim, check it is still current |

When you report a relationship, say which kind it is. "`OrderService` calls
`MatchingEngine.submit`" and "`Instrument` in the client *appears to mirror*
`Instrument` in the server" are different claims.

`impact_of` over-approximates by design: it answers "what could be affected",
not "what will break". Say so rather than presenting it as a failure list.

## Recording what the parser cannot see

When the user explains *why* something is the way it is - a constraint, an
invariant, a trap, a decision - that knowledge dies at the end of the session
unless you write it down. `add_to_graph` puts it in the graph, attached to the
code it is about, and it survives every rebuild.

Do it when the user says "note that", "remember", "be careful with", or explains
a non-obvious reason.

```
add_to_graph(
  target = "example_src/marketd/services/orders.py:120",
  note   = "Risk check through settlement must stay await-free: the single "
           "event-loop thread is what makes this sequence atomic."
)
```

A good note states a constraint or a reason and would change what a future
reader does. A bad note paraphrases the code. If the code already says it,
do not record it.

For relationships between codebases that share no imports - a client and its
server, a schema and its implementation - use `link_locations` with an explicit
relation.

## Keeping it current

The graph rebuilds automatically after Write/Edit. If a tool result looks stale
(a symbol you just added is missing), call `rebuild_graph`; it is local, AST-only,
and takes well under a second on a small repo.
