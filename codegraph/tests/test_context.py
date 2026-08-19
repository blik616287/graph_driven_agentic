"""Prompt detection and context assembly.

Detection precision matters more than recall here. A false positive prepends
irrelevant text to a real user's prompt on every turn; a false negative just
means the agent works the way it did before codegraph existed.
"""

from __future__ import annotations

import pytest

from codegraph.build import write_index
from codegraph.config import Config, Root
from codegraph.context import Index, context_for, detect
from codegraph.model import Graph


@pytest.fixture
def indexed(tmp_path, server_graph):
    graph = server_graph.namespace_to("server")
    config = Config(roots=[Root(name="server", path="server")])
    (tmp_path / "server").mkdir()
    write_index(tmp_path, graph, config)
    return graph, Index.load(tmp_path / ".codegraph" / "index.json")


def test_explicit_path_is_detected(indexed):
    _graph, index = indexed
    found = detect("fix the bug in server/core/book.py please", index)
    assert found.files == ["server/core/book.py"]


def test_bare_filename_is_detected(indexed):
    _graph, index = indexed
    found = detect("what does book.py do?", index)
    assert "server/core/book.py" in found.files


def test_absolute_path_resolves_by_suffix(indexed):
    _graph, index = indexed
    found = detect("look at /home/me/proj/server/core/book.py", index)
    assert found.files == ["server/core/book.py"]


def test_symbol_is_detected(indexed):
    _graph, index = indexed
    found = detect("who calls MatchingEngine?", index)
    assert "MatchingEngine" in found.symbols


def test_backticked_symbols_are_captured(indexed):
    _graph, index = indexed
    found = detect("explain `OrderBook` to me", index)
    assert "OrderBook" in found.symbols
    assert "OrderBook" in found.quoted


def test_directory_names_in_a_path_are_not_symbols(indexed):
    """`server/core/book.py` must not also report 'server' and 'core'."""
    _graph, index = indexed
    found = detect("edit server/core/book.py", index)
    assert found.symbols == []


def test_prose_does_not_trigger(indexed):
    _graph, index = indexed
    for prompt in (
        "what is the weather today",
        "please update the changelog and thanks",
        "can you help me understand this code review process",
    ):
        assert not detect(prompt, index), prompt


def test_unknown_files_do_not_trigger(indexed):
    _graph, index = indexed
    assert not detect("open /etc/passwd and totally_unrelated.py", index)


def test_detection_is_capped(indexed):
    _graph, index = indexed
    prompt = " ".join(["server/core/book.py"] * 20 + ["MatchingEngine"] * 20)
    found = detect(prompt, index, max_files=2, max_symbols=3)
    assert len(found.files) <= 2 and len(found.symbols) <= 3


def test_sentence_labels_are_not_indexed_as_symbols(tmp_path):
    """Backends mint nodes from docstrings; nobody types a whole sentence."""
    graph = Graph()
    graph.add_node("n", label="The next order to fill, discarding tombstones.",
                   source_file="a.py", root="r")
    graph.add_node("m", label="OrderBook", source_file="a.py", root="r")
    write_index(tmp_path, graph, Config(roots=[]))
    index = Index.load(tmp_path / ".codegraph" / "index.json")
    assert "orderbook" in index.symbols
    assert not any(" " in symbol for symbol in index.symbols)


def test_context_reports_dependents_and_dependencies(indexed):
    graph, _index = indexed
    text = context_for(graph, files=["server/core/book.py"])
    assert "server/core/book.py" in text
    assert "OrderBook" in text
    assert "depended on by" in text


def test_context_is_empty_for_unknown_targets(indexed):
    graph, _index = indexed
    assert context_for(graph, files=["nope/missing.py"]) == ""


def test_context_respects_its_budget(indexed):
    graph, _index = indexed
    small = context_for(graph, files=["server/core/book.py"],
                        symbols=["MatchingEngine", "OrderService"], budget=60)
    large = context_for(graph, files=["server/core/book.py"],
                        symbols=["MatchingEngine", "OrderService"], budget=4000)
    assert len(small) < len(large)
    assert "truncated" in small


def test_synthetic_links_are_marked_in_the_output(server_graph, client_graph):
    from codegraph.config import SyntheticRules
    from codegraph.synthetic import link_roots

    graph = Graph.merge(
        [server_graph.namespace_to("server"), client_graph.namespace_to("client")]
    )
    link_roots(graph, SyntheticRules())
    text = context_for(graph, symbols=["Instrument"])
    assert "~" in text
    assert "synthetic" in text.lower()


def test_asserted_notes_are_marked_in_the_output(tmp_path, server_graph):
    from codegraph.annotate import Annotation, AnnotationStore, apply
    import time

    graph = server_graph.namespace_to("server")
    store = AnnotationStore(tmp_path)
    store.append(Annotation(id="ann_x", kind="note", target="server/core/book.py",
                            text="the heap is pruned lazily", created_at=time.time()))
    apply(graph, store.all())

    text = context_for(graph, files=["server/core/book.py"])
    assert "asserted notes" in text
    assert "pruned lazily" in text


def test_prose_nodes_are_excluded_from_the_defines_line(tmp_path):
    """A docstring node is not a definition, and crowds out the ones that are."""
    graph = Graph()
    graph.add_node("cls", label="OrderBook", source_file="a.py", source_location="L10",
                   root="r", file_type="code", _callable_class=True)
    graph.add_node("doc", label="The next order to fill, discarding tombstones.",
                   source_file="a.py", source_location="L11", root="r", file_type="code")
    text = context_for(graph, files=["a.py"])
    assert "OrderBook" in text
    assert "discarding tombstones" not in text
