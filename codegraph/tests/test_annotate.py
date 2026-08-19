"""Durable annotations.

The property under test throughout: a fact asserted by a human survives the
rebuild that the edit hook triggers seconds later.
"""

from __future__ import annotations

import time

from codegraph.annotate import Annotation, AnnotationStore, apply
from codegraph.model import Graph


def store_for(tmp_path) -> AnnotationStore:
    return AnnotationStore(tmp_path)


def note(store, target, text="a fact", **kwargs) -> Annotation:
    annotation = Annotation(
        id=store.next_id(), kind=kwargs.pop("kind", "note"), target=target,
        text=text, created_at=time.time(), **kwargs,
    )
    time.sleep(0.002)  # ids are millisecond-stamped; keep them distinct
    return store.append(annotation)


def test_note_becomes_a_searchable_node(tmp_path, server_graph):
    graph = server_graph.namespace_to("server")
    store = store_for(tmp_path)
    note(store, "server/core/book.py", "the heap is pruned lazily")
    result = apply(graph, store.all())

    assert result["applied"] == 1
    notes = [n for n in graph.nodes.values() if n.get("_origin") == "codegraph-annotation"]
    assert len(notes) == 1
    assert "lazily" in notes[0]["text"]
    # Searchable, not buried in an attribute nothing indexes.
    assert graph.find_symbol("the heap is pruned lazily")


def test_line_target_resolves_to_the_enclosing_definition(tmp_path, server_graph):
    """A note on line 45 belongs to the function that starts at 40."""
    graph = server_graph.namespace_to("server")
    store = store_for(tmp_path)
    note(store, "server/core/book.py:45", "inside OrderBook")
    apply(graph, store.all())

    note_node = next(n for n in graph.nodes.values() if n.get("_origin") == "codegraph-annotation")
    attached = [e["target"] for e in graph.outgoing(note_node["id"])]
    assert graph.nodes[attached[0]]["label"] == "OrderBook"


def test_annotations_survive_a_rebuild(tmp_path, server_graph):
    store = store_for(tmp_path)
    note(store, "server/core/book.py", "survives")

    for _ in range(3):
        fresh = server_graph.namespace_to("server")  # a brand-new graph each time
        result = apply(fresh, store.all())
        assert result["applied"] == 1
        assert any(n.get("_origin") == "codegraph-annotation" for n in fresh.nodes.values())


def test_reapplying_does_not_duplicate(tmp_path, server_graph):
    graph = server_graph.namespace_to("server")
    store = store_for(tmp_path)
    note(store, "server/core/book.py", "once")
    apply(graph, store.all())
    apply(graph, store.all())
    notes = [n for n in graph.nodes.values() if n.get("_origin") == "codegraph-annotation"]
    assert len(notes) == 1


def test_orphans_are_reported_not_silently_dropped(tmp_path, server_graph):
    """The code moved. The assertion may still be true - say so."""
    graph = server_graph.namespace_to("server")
    store = store_for(tmp_path)
    note(store, "server/deleted/gone.py", "about vanished code")
    result = apply(graph, store.all())

    assert result["applied"] == 0
    assert len(result["orphaned"]) == 1
    assert result["orphaned"][0]["target"] == "server/deleted/gone.py"


def test_orphan_reattaches_when_the_target_returns(tmp_path, server_graph):
    store = store_for(tmp_path)
    note(store, "server/core/late.py", "arrives later")
    assert apply(server_graph.namespace_to("server"), store.all())["applied"] == 0

    later = server_graph.namespace_to("server")
    later.add_node("server::late", label="Late", source_file="server/core/late.py",
                   source_location="L1", root="server")
    assert apply(later, store.all())["applied"] == 1


def test_link_annotations_create_asserted_edges(tmp_path, server_graph, client_graph):
    graph = Graph.merge(
        [server_graph.namespace_to("server"), client_graph.namespace_to("client")]
    )
    store = store_for(tmp_path)
    note(store, "TradingClient", kind="link", links_to="OrderService",
         relation="calls_over_http", text="the only wire boundary")
    apply(graph, store.all())

    edges = [e for e in graph.edges if e.get("relation") == "calls_over_http"]
    assert edges and edges[0]["confidence"] == "ASSERTED"


def test_revoked_annotations_disappear(tmp_path, server_graph):
    store = store_for(tmp_path)
    annotation = note(store, "server/core/book.py", "temporary")
    assert len(store.all()) == 1

    assert store.revoke(annotation.id) is True
    assert store.all() == []

    graph = server_graph.namespace_to("server")
    assert apply(graph, store.all())["applied"] == 0


def test_revoking_an_unknown_id_is_false(tmp_path):
    assert store_for(tmp_path).revoke("ann_nope") is False


def test_log_is_append_only_and_last_write_wins(tmp_path, server_graph):
    store = store_for(tmp_path)
    annotation = note(store, "server/core/book.py", "first version")
    annotation.text = "second version"
    store.append(annotation)

    entries = store.all()
    assert len(entries) == 1
    assert entries[0].text == "second version"
    # Both writes are still on disk; nothing was rewritten in place.
    assert len(store.path.read_text().strip().splitlines()) == 2


def test_a_corrupt_line_does_not_lose_the_rest(tmp_path, server_graph):
    store = store_for(tmp_path)
    note(store, "server/core/book.py", "good one")
    with store.path.open("a") as handle:
        handle.write("{ this is not json\n")
    note(store, "server/core/matching.py", "another good one")

    assert len(store.all()) == 2


def test_line_target_prefers_a_definition_over_a_docstring_node(tmp_path):
    """Backends mint nodes for docstrings, which sit just below the def line.

    A note on a line inside a function body is nearest to the docstring node,
    so without a preference for real definitions it anchors to a blob of prose
    instead of to the code it is about.
    """
    from conftest import make_graph

    graph = make_graph([("submit", "submit", "core/matching.py", 72)], file_prefix="server/")
    graph.add_node(
        "doc",
        label="Match ``order`` against the book, then rest or cancel.",
        source_file="server/core/matching.py",
        source_location="L73",
        file_type="code",
    )
    store = store_for(tmp_path)
    note(store, "server/core/matching.py:90", "FOK is checked before any unit moves")
    apply(graph, store.all())

    note_node = next(n for n in graph.nodes.values() if n.get("_origin") == "codegraph-annotation")
    anchors = [graph.nodes[e["target"]]["label"] for e in graph.outgoing(note_node["id"])]
    assert anchors == ["submit"]
