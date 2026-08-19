<!-- codegraph:begin -->
## codegraph

A deterministic AST knowledge graph of this repository is available over MCP.
It is built from tree-sitter parsing only - no model calls - and a hook keeps it
current as files change.

Use it instead of reading whole files to orient yourself:

- `context_for_paths` - before editing a file: what it defines, what depends on
  it, what it depends on, and any recorded notes. Start here.
- `impact_of` - before changing a shared symbol: the blast radius.
- `shortest_path` - how two parts of the system connect.
- `find_symbol` / `get_node` - locate a definition and its edges.
- `add_to_graph` - record a decision or constraint the parser cannot see, so it
  survives for the next session. Do this when the user explains *why*.
- `link_locations` - assert a relationship across repositories.

Edges are labelled with their origin: `ast` was parsed and is fact,
`codegraph-synthetic` was inferred by a heuristic, `codegraph-annotation` was
asserted by a person. Weigh them accordingly.
<!-- codegraph:end -->
