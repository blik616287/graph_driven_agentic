# codegraph: instructions for an agent

A deterministic AST knowledge graph of this project is available over MCP. It is
rebuilt automatically after every edit.

## Reach for it before reading files

Reading a file shows what it contains. The graph shows **what depends on it** -
which is what determines whether a change is safe, and the one thing you cannot
learn by reading the file.

- `context_for_paths` - before editing: definitions, dependents, dependencies,
  recorded notes.
- `impact_of` - before changing a shared symbol: the blast radius.
- `shortest_path` - how two parts of the system connect.
- `find_symbol` / `get_node` - locate a definition and its edges.

## Weigh edges by origin

| origin | meaning | treat as |
|---|---|---|
| `ast` | parsed from source | fact |
| `codegraph-synthetic` | inferred across roots by a heuristic | a lead to verify |
| `codegraph-annotation` | asserted by a person or agent | a claim to trust but date-check |

Say which kind you are relying on. `impact_of` deliberately over-approximates:
report it as "could be affected", never as a list of guaranteed breakages.

## Record what the parser cannot see

When the user explains *why* - a constraint, an invariant, a trap - call
`add_to_graph` with the location and the fact. It survives every rebuild, so the
next session starts with it. Record reasons and constraints; do not paraphrase
code that already says it.

Use `link_locations` for relationships between codebases that share no imports.

## When the graph is wrong

The file on disk always wins. If a result looks stale, call `rebuild_graph` -
it is local, AST-only, and sub-second on a small repository.
