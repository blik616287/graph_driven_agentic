"""codegraph - a deterministic, multi-root code knowledge graph for coding agents.

Three ideas hold the design together:

**Deterministic.**  The graph is built from AST parsing only.  No LLM is ever
called on the build path, so the same commit always produces the same graph and
a rebuild triggered by a file-edit hook can never cost money or drift.

**Multi-root.**  Several codebases can live in one graph, kept apart by an id
namespace and joined by *synthetic* edges - inferred (shared symbol names,
mirrored paths) or declared by hand.  Synthetic edges are tagged, so a consumer
can always tell what was parsed from what was guessed.

**Dependency-split.**  Building needs a backend (graphify / code-review-graph)
and its tree-sitter grammars.  Serving the graph over MCP, and the two agent
hooks, are pure standard library - they only read JSON.  That is what lets the
hooks run on whatever Python the agent happens to have.
"""

from __future__ import annotations

__version__ = "0.3.0"

SYNTHETIC_ORIGIN = "codegraph-synthetic"
ANNOTATION_ORIGIN = "codegraph-annotation"
BACKEND_ORIGINS = ("ast", "graphify", "code-review-graph")

__all__ = ["__version__", "SYNTHETIC_ORIGIN", "ANNOTATION_ORIGIN", "BACKEND_ORIGINS"]
