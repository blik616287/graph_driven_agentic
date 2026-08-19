"""The graph model: namespacing, merging, traversal."""

from __future__ import annotations


from codegraph.model import Graph, namespaced, normalise_path, split_namespace


def test_namespacing_keeps_same_named_symbols_apart(server_graph, client_graph):
    """The whole point of namespacing, stated as a test."""
    merged = Graph.merge([server_graph.namespace_to("server"), client_graph.namespace_to("client")])
    assert "server::order" in merged.nodes
    assert "client::corder" in merged.nodes
    # Two roots both defining Order must remain two nodes.
    orders = [n for n in merged.nodes.values() if n.get("label") == "Order"]
    assert len(orders) == 2
    assert {n["root"] for n in orders} == {"server", "client"}


def test_namespacing_is_idempotent():
    assert namespaced("a", "a::x") == "a::x"
    assert split_namespace("a::x") == ("a", "x")
    assert split_namespace("x") == ("", "x")


def test_edges_are_rewritten_with_their_nodes(server_graph):
    namespaced_graph = server_graph.namespace_to("server")
    for edge in namespaced_graph.edges:
        assert edge["source"].startswith("server::")
        assert edge["target"].startswith("server::")


def test_dangling_edge_targets_stay_attributed_to_their_root():
    """A backend can emit an edge to something it never indexed."""
    graph = Graph()
    graph.add_node("a", label="A", source_file="a.py")
    graph.add_edge("a", "never_indexed", "calls")
    namespaced_graph = graph.namespace_to("svc")
    assert namespaced_graph.edges[0]["target"] == "svc::never_indexed"


def test_blast_radius_walks_dependencies_backwards(server_graph):
    graph = server_graph.namespace_to("server")
    affected = graph.blast_radius("server::book", depth=2)
    labels = {entry["label"] for entry in affected}
    # engine calls book, and svc calls engine - both are downstream of a change.
    assert "MatchingEngine" in labels
    assert "OrderService" in labels
    # Distance is recorded so a caller can prefer the near ones.
    assert {e["label"]: e["distance"] for e in affected}["MatchingEngine"] == 1


def test_blast_radius_is_capped(server_graph):
    graph = server_graph.namespace_to("server")
    assert len(graph.blast_radius("server::book", depth=5, limit=2)) == 2


def test_blast_radius_ignores_non_dependency_edges():
    graph = Graph()
    graph.add_node("a", label="A", source_file="a.py")
    graph.add_node("note", label="Note", source_file="")
    graph.add_edge("note", "a", "annotates")
    assert graph.blast_radius("a") == []


def test_shortest_path_is_undirected(server_graph):
    graph = server_graph.namespace_to("server")
    path = graph.shortest_path("server::svc", "server::book")
    assert path[0] == "server::svc" and path[-1] == "server::book"
    # Reachable in the other direction too, despite edge direction.
    assert graph.shortest_path("server::book", "server::svc")


def test_shortest_path_returns_empty_when_unreachable(server_graph):
    graph = server_graph.namespace_to("server")
    graph.add_node("island", label="Island", source_file="x.py")
    assert graph.shortest_path("server::svc", "island") == []


def test_file_lookup_prefers_exact_over_suffix(server_graph):
    graph = server_graph.namespace_to("server")
    exact = graph.nodes_in_file("server/core/book.py")
    assert exact == graph.nodes_in_file("/abs/prefix/server/core/book.py")
    assert "server::book" in exact


def test_find_symbol_puts_exact_matches_first(server_graph):
    graph = server_graph.namespace_to("server")
    found = graph.find_symbol("Order")
    assert graph.nodes[found[0]]["label"] == "Order"


def test_save_is_atomic_and_round_trips(tmp_path, server_graph):
    target = tmp_path / "nested" / "graph.json"
    server_graph.save(target)
    assert target.exists()
    assert not target.with_suffix(".json.tmp").exists()  # staging file cleaned up
    reloaded = Graph.load(target)
    assert reloaded.nodes.keys() == server_graph.nodes.keys()
    assert len(reloaded.edges) == len(server_graph.edges)


def test_accepts_networkx_edges_key(tmp_path):
    """networkx renamed 'links' to 'edges'; both must load."""
    payload = {"nodes": [{"id": "a", "label": "A"}], "edges": [{"source": "a", "target": "a", "relation": "self"}]}
    graph = Graph.from_node_link(payload)
    assert len(graph.edges) == 1


def test_indexes_invalidate_on_write(server_graph):
    before = len(server_graph.file_index)
    server_graph.add_node("new", label="New", source_file="server/new.py")
    assert len(server_graph.file_index) == before + 1


def test_normalise_path_handles_separators_and_prefixes():
    assert normalise_path("./a/b.py") == "a/b.py"
    assert normalise_path("a\\b.py") == "a/b.py"
    assert normalise_path("a/b/") == "a/b"
