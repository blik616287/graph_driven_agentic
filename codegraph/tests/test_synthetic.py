"""Synthetic cross-root edges.

The rules are heuristics, so most of these tests assert what codegraph
*refuses* to link. A missing edge costs a little recall; a wrong edge invents a
relationship the agent will then reason from.
"""

from __future__ import annotations

import pytest

from codegraph.config import SyntheticRules
from codegraph.model import Graph
from codegraph.synthetic import (
    clear_synthetic,
    link_roots,
    normalise_symbol,
    root_prefixes,
    symbol_bucket,
)

from conftest import make_graph


def merged(server_graph, client_graph) -> Graph:
    return Graph.merge(
        [server_graph.namespace_to("server"), client_graph.namespace_to("client")]
    )


def synthetic_edges(graph: Graph) -> list[dict]:
    return [e for e in graph.edges if e.get("_origin") == "codegraph-synthetic"]


def test_shared_domain_types_are_linked(server_graph, client_graph):
    graph = merged(server_graph, client_graph)
    link_roots(graph, SyntheticRules())
    linked = {
        graph.nodes[e["source"]]["label"]
        for e in synthetic_edges(graph)
        if e.get("rule") == "shared_symbol"
    }
    assert {"Order", "Instrument"} <= linked


def test_synthetic_edges_are_labelled_as_such(server_graph, client_graph):
    graph = merged(server_graph, client_graph)
    link_roots(graph, SyntheticRules())
    for edge in synthetic_edges(graph):
        assert edge["confidence"] == "SYNTHETIC"
        assert edge["_origin"] == "codegraph-synthetic"
        assert edge["rule"] and edge["evidence"]


def test_a_class_does_not_match_a_same_named_function(server_graph, client_graph):
    """`Order` the class must not collide with `order()` the test helper.

    Normalisation strips `()`, so without kind-bucketing the two share a key,
    the ambiguity guard fires, and the real domain type goes unlinked.
    """
    graph = merged(server_graph, client_graph)
    link_roots(graph, SyntheticRules())
    orders = [
        e for e in synthetic_edges(graph)
        if graph.nodes[e["source"]]["label"] == "Order"
    ]
    assert orders, "Order should still link despite the order() helper"
    assert symbol_bucket({"_callable_class": True}) != symbol_bucket({"_callable": True})


@pytest.mark.parametrize("label,expected", [
    ("main()", "main"), (".revoke()", "revoke"), ("book.py", "book"),
    ("OrderBook", "OrderBook"), ("__init__", "__init__"),
])
def test_normalise_symbol_strips_decoration(label, expected):
    assert normalise_symbol(label) == expected


def test_generic_names_are_not_linked():
    """`main`, `config`, `__init__` appear everywhere and connect nothing."""
    server = make_graph([("a", "main", "a.py", 1), ("b", "Config", "b.py", 1)], file_prefix="s/")
    client = make_graph([("c", "main", "c.py", 1), ("d", "Config", "d.py", 1)], file_prefix="c/")
    graph = Graph.merge([server.namespace_to("s"), client.namespace_to("c")])
    link_roots(graph, SyntheticRules())
    assert synthetic_edges(graph) == []


def test_ambiguous_names_are_not_linked():
    """A name defined twice inside one root is already ambiguous there."""
    server = make_graph(
        [("a", "Widget", "a.py", 1), ("b", "Widget", "b.py", 1)], file_prefix="s/"
    )
    client = make_graph([("c", "Widget", "c.py", 1)], file_prefix="c/")
    graph = Graph.merge([server.namespace_to("s"), client.namespace_to("c")])
    link_roots(graph, SyntheticRules())
    assert synthetic_edges(graph) == []


def test_bare_method_names_are_not_linked():
    """Two unrelated classes may both have a `.reset()`."""
    server = make_graph([("a", ".resetting()", "a.py", 1)], file_prefix="s/")
    client = make_graph([("c", ".resetting()", "c.py", 1)], file_prefix="c/")
    graph = Graph.merge([server.namespace_to("s"), client.namespace_to("c")])
    link_roots(graph, SyntheticRules())
    assert synthetic_edges(graph) == []


def test_short_names_are_not_linked():
    server = make_graph([("a", "Node", "a.py", 1)], file_prefix="s/")
    client = make_graph([("c", "Node", "c.py", 1)], file_prefix="c/")
    graph = Graph.merge([server.namespace_to("s"), client.namespace_to("c")])
    link_roots(graph, SyntheticRules(min_symbol_length=5))
    assert synthetic_edges(graph) == []


def test_mirrored_paths_are_compared_relative_to_each_root():
    """Roots checked out at different depths still correspond."""
    server = make_graph([("a", "Alpha", "api/orders.py", 1)], file_prefix="deep/nested/server/")
    client = make_graph([("c", "Gamma", "api/orders.py", 1)], file_prefix="client/")
    graph = Graph.merge([server.namespace_to("s"), client.namespace_to("c")])
    prefixes = root_prefixes(graph)
    assert prefixes["s"] == "deep/nested/server/api"
    link_roots(graph, SyntheticRules(shared_symbol=False))
    assert [e["rule"] for e in synthetic_edges(graph)] == ["mirrored_path"]


def test_ceremony_filenames_do_not_mirror():
    """Every Python package has an __init__.py; that is not correspondence."""
    server = make_graph([("a", "Alpha", "pkg/__init__.py", 1)], file_prefix="s/")
    client = make_graph([("c", "Gamma", "pkg/__init__.py", 1)], file_prefix="c/")
    graph = Graph.merge([server.namespace_to("s"), client.namespace_to("c")])
    link_roots(graph, SyntheticRules(shared_symbol=False))
    assert synthetic_edges(graph) == []


def test_explicit_links_are_asserted_not_synthetic(server_graph, client_graph):
    graph = merged(server_graph, client_graph)
    link_roots(graph, SyntheticRules(
        shared_symbol=False, mirrored_path=False,
        explicit=[{"source": "TradingClient", "target": "OrderService",
                   "relation": "calls_over_http", "note": "the client's only entry point"}],
    ))
    edges = synthetic_edges(graph)
    assert len(edges) == 1
    assert edges[0]["confidence"] == "ASSERTED"
    assert edges[0]["relation"] == "calls_over_http"
    assert "entry point" in edges[0]["evidence"]


def test_explicit_links_resolve_files_and_symbols(server_graph, client_graph):
    graph = merged(server_graph, client_graph)
    link_roots(graph, SyntheticRules(
        shared_symbol=False, mirrored_path=False,
        explicit=[{"source": "client/trading.py", "target": "server/services/orders.py",
                   "relation": "talks_to"}],
    ))
    assert len(synthetic_edges(graph)) >= 1


def test_unresolvable_explicit_links_are_skipped_not_fatal(server_graph, client_graph):
    graph = merged(server_graph, client_graph)
    link_roots(graph, SyntheticRules(
        shared_symbol=False, mirrored_path=False,
        explicit=[{"source": "NoSuchThing", "target": "AlsoMissing", "relation": "x"}],
    ))
    assert synthetic_edges(graph) == []


def test_rebuild_clears_stale_synthetic_edges(server_graph, client_graph):
    """A renamed symbol must not keep its cross-root edge forever."""
    graph = merged(server_graph, client_graph)
    link_roots(graph, SyntheticRules())
    assert synthetic_edges(graph)
    # Second pass with the rules off should leave none behind.
    link_roots(graph, SyntheticRules(shared_symbol=False, mirrored_path=False))
    assert synthetic_edges(graph) == []


def test_clear_synthetic_leaves_parsed_edges_alone(server_graph, client_graph):
    graph = merged(server_graph, client_graph)
    link_roots(graph, SyntheticRules())
    parsed_before = len([e for e in graph.edges if e.get("_origin") == "ast"])
    clear_synthetic(graph)
    assert len([e for e in graph.edges if e.get("_origin") == "ast"]) == parsed_before


def test_single_root_produces_no_synthetic_edges(server_graph):
    graph = server_graph.namespace_to("server")
    counts = link_roots(graph, SyntheticRules())
    assert counts["shared_symbol"] == 0 and counts["mirrored_path"] == 0
