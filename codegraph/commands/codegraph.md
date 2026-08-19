---
name: codegraph
description: Build, inspect, extend, or explain the deterministic code knowledge graph. Use for "/codegraph build", "/codegraph status", "add this to the graph", "link these two repos", or any question about how parts of the codebase connect.
---

# codegraph

You are operating the codegraph workspace for this project. The graph is a
deterministic AST index of one or more codebases, served over MCP.

## First, orient

Call `graph_stats`. It tells you what is indexed, how stale the graph is, and
which symbols are hubs. If it reports no graph, run the build (below).

## What the user asked

`$ARGUMENTS`

Match it to one of these:

### "build" / "rebuild" / "index this"
Run the build. Prefer the `rebuild_graph` MCP tool. If it is unavailable, run:

```bash
./codegraph/scripts/codegraph build
```

Add `--incremental` to re-parse only changed files. Report node/edge counts, any
synthetic edges, and any orphaned annotations.

### "status" / "what's indexed"
`graph_stats` plus `list_roots`. Report roots, counts, graph age, and the split
of edge origins (`ast` vs `codegraph-synthetic` vs `codegraph-annotation`).

### "add <path or area>" / "add this to the graph" / "remember that ..."
This is the interactive-extension path. The user is telling you something the
parser cannot derive - a constraint, a reason, an invariant, a gotcha.

1. Resolve what they mean by "this". If they are pointing at the current file
   or a line range, use `path/to/file.py:LINE`.
2. Call `add_to_graph` with a `target` and a `note`.
3. Write the note as a **fact a future reader needs**, not a restatement of the
   code. "Consumes the heap; never call from a read path" earns its place;
   "iterates over levels" does not.
4. Confirm what was attached and to which node.

If they are asserting a relationship between two places - especially across
repositories - use `link_locations` with an explicit `relation` such as
`implements`, `calls_over_http`, or `generated_from`.

### "link <A> and <B>" / "these two repos are related"
Use `link_locations`. If they want the whole codebase indexed rather than a
single link, add a root instead:

```bash
./codegraph/scripts/codegraph add-root <path> --name <name>
./codegraph/scripts/codegraph build
```

Then report which synthetic edges were inferred, and their rules.

### "what depends on X" / "what breaks if I change X"
`impact_of`. Report the affected files first, then the closest symbols. State
the depth you searched and that the result is a conservative over-approximation.

### "how does X connect to Y"
`shortest_path`. Print the chain with each hop's relation, and flag any hop
whose origin is not `ast` - those are inferred, not parsed.

### anything else about structure
`context_for_paths` with the user's text as `prompt`; it detects paths and
symbols itself.

## Rules

- **Never present a synthetic or asserted edge as a parsed fact.** Every tool
  result carries `origin` and `confidence`. Say which you are relying on.
- **The graph is a map, not the territory.** When it disagrees with the file on
  disk, the file wins - say so and suggest a rebuild.
- **Do not dump raw tool JSON.** Answer the question; cite files and line
  numbers as `path:line`.
