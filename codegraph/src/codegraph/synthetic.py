"""Synthetic edges: linking roots that share no imports.

Two codebases indexed together have no parsed edges between them - nothing in
one repo *imports* the other.  But they are often related: a client and its
server, a service and its infrastructure, an implementation and its port.
Synthetic edges make that relationship queryable.

Every synthetic edge is tagged ``_origin: "codegraph-synthetic"`` with
``confidence: "SYNTHETIC"`` and a ``rule`` naming the heuristic that produced
it, so a consumer can always separate "the parser read this" from "codegraph
guessed this".  That distinction is the whole reason the feature is safe.

The heuristics are intentionally conservative, because a wrong synthetic edge is
worse than a missing one - it invents a relationship an agent will then reason
from:

``shared_symbol``   the same distinctive symbol name defined in two roots.
                    Short names and names defined in many places are skipped;
                    ``Config`` appearing in six roots links nothing useful.
``mirrored_path``   the same relative file path under two roots, e.g.
                    ``api/orders.py`` in both a server and a generated client.
"""

from __future__ import annotations

from collections import defaultdict

from . import SYNTHETIC_ORIGIN
from .config import SyntheticRules
from .model import Graph, normalise_path

# Filenames that exist in every project of a given language. Two roots both
# having one says nothing about whether they correspond.
CEREMONY_FILENAMES = frozenset({
    "__init__.py", "__main__.py", "conftest.py", "setup.py", "index.js", "index.ts",
    "mod.rs", "lib.rs", "main.rs", "main.go", "doc.go", "package.json", "tsconfig.json",
    "readme.md", "license", "makefile", "dockerfile", ".gitignore",
})

# Names that appear in nearly every codebase carry no cross-root signal.
STOPWORD_SYMBOLS = frozenset({
    "main", "run", "test", "setup", "init", "config", "settings", "client", "server",
    "handler", "handlers", "utils", "util", "helper", "helpers", "base", "common",
    "model", "models", "index", "app", "error", "errors", "logger", "start", "stop",
    "close", "get", "set", "load", "save", "build", "create", "update", "delete",
    "read", "write", "parse", "format", "validate", "process", "execute", "call",
})


def clear_synthetic(graph: Graph) -> int:
    """Drop every previously generated synthetic edge.

    Rebuilds regenerate these from scratch.  Keeping the old ones would let a
    renamed or deleted symbol keep a cross-root relationship indefinitely.
    Explicit (human-asserted) links are stored in config and re-applied, so
    clearing here does not lose them.
    """
    return graph.drop_edges(lambda edge: edge.get("_origin") == SYNTHETIC_ORIGIN)


def link_roots(graph: Graph, rules: SyntheticRules) -> dict[str, int]:
    """Add synthetic edges across roots.  Returns a count per rule."""
    clear_synthetic(graph)
    counts = {"shared_symbol": 0, "mirrored_path": 0, "explicit": 0}
    if len(graph.roots()) > 1:
        if rules.shared_symbol:
            counts["shared_symbol"] = _link_shared_symbols(graph, rules)
        if rules.mirrored_path:
            counts["mirrored_path"] = _link_mirrored_paths(graph)
    counts["explicit"] = _link_explicit(graph, rules)
    return counts


def normalise_symbol(label: str) -> str:
    """Reduce a backend label to the bare name a match should be judged on.

    Backends decorate labels: ``main()`` for callables, ``.revoke()`` for
    methods, ``book.py`` for file nodes.  Comparing the decorated forms lets
    ``main()`` slip past a stopword list containing ``main`` - which is exactly
    the bug this function exists to prevent.
    """
    name = label.strip()
    if name.endswith("()"):
        name = name[:-2]
    name = name.lstrip(".")
    if "." in name and not name.startswith("_"):
        # A file node ("book.py") or dotted path: keep the last segment.
        head, _, extension = name.rpartition(".")
        if head and extension.isalpha() and len(extension) <= 4:
            name = head
    return name.strip()


def symbol_bucket(node: dict) -> str:
    """Coarse kind for a node, used to keep unlike symbols from matching.

    Stripping ``()`` during normalisation makes the class ``Order`` and the test
    helper ``order()`` share a key.  They then look like two definitions of one
    name inside a root, the ambiguity guard fires, and a genuinely shared domain
    type goes unlinked.  Bucketing by kind keeps them apart.
    """
    if node.get("_callable_class") or node.get("kind") in ("class", "interface", "struct"):
        return "class"
    if node.get("_callable") or node.get("kind") in ("function", "method"):
        return "callable"
    return str(node.get("file_type") or "other")


def _is_linkable_symbol(node: dict, normalised: str, rules: SyntheticRules) -> bool:
    """Is this symbol distinctive enough that sharing a name means something?"""
    if len(normalised) < rules.min_symbol_length:
        return False
    if normalised.lower() in STOPWORD_SYMBOLS:
        return False
    # Dunders are language ceremony: every package has __init__ and __main__.
    if normalised.startswith("__") and normalised.endswith("__"):
        return False
    if not node.get("source_file"):
        return False  # an unresolved reference, not a definition
    label = str(node.get("label") or "")
    # Bare method names (".revoke()") are matched without their class, so two
    # unrelated classes with a same-named method would be linked. Too weak.
    if label.startswith("."):
        return False
    # File-to-file correspondence is the mirrored_path rule's job; letting this
    # rule also match filenames produces a duplicate edge with worse evidence.
    return not (
        node.get("file_type") == "code"
        and label.lower().endswith(
            (".py", ".ts", ".js", ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".md")
        )
    )


def _link_shared_symbols(graph: Graph, rules: SyntheticRules) -> int:
    """Connect identically named symbols that live in different roots."""
    by_label: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for node_id, node in graph.nodes.items():
        normalised = normalise_symbol(str(node.get("label") or ""))
        if not _is_linkable_symbol(node, normalised, rules):
            continue
        by_label[(normalised.lower(), symbol_bucket(node))][node.get("root", "")].append(node_id)

    added = 0
    seen: set[tuple[str, str]] = set()
    for (label, _bucket), per_root in by_label.items():
        if len(per_root) < 2:
            continue
        total = sum(len(ids) for ids in per_root.values())
        if total > rules.max_symbol_fanout:
            # A name this common is shared vocabulary, not a connection.
            continue
        # More than one definition per root means the name is ambiguous even
        # inside a single codebase; linking it across roots compounds the guess.
        if any(len(ids) > 1 for ids in per_root.values()):
            continue
        roots = sorted(per_root)
        for i, left_root in enumerate(roots):
            for right_root in roots[i + 1 :]:
                left, right = per_root[left_root][0], per_root[right_root][0]
                key = (left, right)
                if key in seen:
                    continue
                seen.add(key)
                graph.add_edge(
                    left, right, "mirrors",
                    confidence="SYNTHETIC",
                    origin=SYNTHETIC_ORIGIN,
                    rule="shared_symbol",
                    evidence=f"both roots define {label!r}",
                    weight=0.5,
                )
                added += 1
    return added


def root_prefixes(graph: Graph) -> dict[str, str]:
    """Longest common directory prefix of each root's files.

    Paths in the graph are workspace-relative, so two roots checked out at
    different depths never look alike: ``example_src/marketd/core/models.py``
    against ``vendor/client/marketd/core/models.py``.  Stripping each root's own
    prefix first is what makes "the same file in both" a question about the
    repositories rather than about where they happen to sit on disk.

    Derived from the graph rather than from config so it stays correct when a
    root's files live under a subdirectory of the configured path.
    """
    per_root: dict[str, list[list[str]]] = defaultdict(list)
    for node in graph.nodes.values():
        source_file = node.get("source_file")
        if source_file:
            per_root[node.get("root", "")].append(normalise_path(source_file).split("/")[:-1])

    prefixes: dict[str, str] = {}
    for root, directories in per_root.items():
        if not directories:
            continue
        common = directories[0]
        for parts in directories[1:]:
            limit = min(len(common), len(parts))
            cut = limit
            for index in range(limit):
                if common[index] != parts[index]:
                    cut = index
                    break
            common = common[:cut]
            if not common:
                break
        prefixes[root] = "/".join(common)
    return prefixes


def _relative_to_root(path: str, root: str, prefixes: dict[str, str]) -> str:
    prefix = prefixes.get(root, "")
    cleaned = normalise_path(path)
    if prefix and cleaned.startswith(prefix + "/"):
        return cleaned[len(prefix) + 1 :]
    return cleaned


def _link_mirrored_paths(graph: Graph) -> int:
    """Connect files at the same root-relative path under different roots."""
    prefixes = root_prefixes(graph)
    by_relative: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for node_id, node in graph.nodes.items():
        source_file = node.get("source_file")
        if not source_file:
            continue
        root = node.get("root", "")
        relative = _relative_to_root(str(source_file), root, prefixes)
        if relative.rsplit("/", 1)[-1].lower() in CEREMONY_FILENAMES:
            continue
        by_relative[relative][root].add(node_id)

    added = 0
    for relative, per_root in by_relative.items():
        if len(per_root) < 2:
            continue
        roots = sorted(per_root)
        # One representative node per (root, file) keeps this O(roots^2) rather
        # than O(symbols^2) - the edge means "these files correspond", and
        # duplicating it per symbol would drown the graph.
        for i, left_root in enumerate(roots):
            for right_root in roots[i + 1 :]:
                left = min(per_root[left_root])
                right = min(per_root[right_root])
                graph.add_edge(
                    left, right, "parallels",
                    confidence="SYNTHETIC",
                    origin=SYNTHETIC_ORIGIN,
                    rule="mirrored_path",
                    evidence=f"both roots contain {relative}",
                    weight=0.4,
                )
                added += 1
    return added


def _link_explicit(graph: Graph, rules: SyntheticRules) -> int:
    """Apply hand-declared links from config.

    These are assertions, not guesses, so they get ``confidence: ASSERTED``.
    Endpoints are resolved leniently (id, then symbol name, then file path)
    because a human writing config should not have to know node id syntax.
    """
    added = 0
    for entry in rules.explicit:
        sources = _resolve_endpoint(graph, entry.get("source", ""))
        targets = _resolve_endpoint(graph, entry.get("target", ""))
        relation = entry.get("relation", "relates_to")
        if not sources or not targets:
            continue
        for source in sources:
            for target in targets:
                if source == target or graph.has_edge(source, target, relation):
                    continue
                graph.add_edge(
                    source, target, relation,
                    confidence="ASSERTED",
                    origin=SYNTHETIC_ORIGIN,
                    rule="explicit",
                    evidence=entry.get("note", "declared in codegraph config"),
                    weight=1.0,
                )
                added += 1
    return added


def _resolve_endpoint(graph: Graph, reference: str, limit: int = 4) -> list[str]:
    """Resolve a user-written reference to node ids.

    Order matters: an exact id is certain, a file path is nearly certain, and a
    symbol name is a search.  Trying them in that order means a precise
    reference is never widened into a fuzzy match.
    """
    if not reference:
        return []
    if reference in graph.nodes:
        return [reference]
    in_file = graph.nodes_in_file(reference)
    if in_file:
        return in_file[:limit]
    return graph.find_symbol(reference, limit=limit)


def describe(counts: dict[str, int]) -> str:
    parts = [f"{name}={count}" for name, count in counts.items() if count]
    return ", ".join(parts) if parts else "none"
