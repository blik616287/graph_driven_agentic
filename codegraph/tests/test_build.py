"""Build orchestration: merge, path rewriting, failure handling."""

from __future__ import annotations

import json

import pytest

from codegraph.backends.base import BackendError
from codegraph.build import build, index_path, load_state
from codegraph.config import Config, Root
from codegraph.model import Graph
from conftest import FakeBackend


def test_build_merges_roots_and_links_them(two_roots):
    workspace, config = two_roots
    report = build(workspace, config)

    assert report.nodes == 12          # 8 server + 4 client
    assert set(report.roots) == {"server", "client"}
    assert report.synthetic["shared_symbol"] >= 2
    assert report.errors == []

    graph = Graph.load(report.graph_path)
    assert {n["root"] for n in graph.nodes.values()} == {"server", "client"}


def test_build_writes_the_detection_index(two_roots):
    workspace, config = two_roots
    build(workspace, config)

    payload = json.loads(index_path(workspace).read_text())
    assert payload["counts"]["nodes"] == 12
    assert "orderbook" in payload["symbols"]
    assert any(path.endswith("core/book.py") for path in payload["files"])


def test_build_records_state(two_roots):
    workspace, config = two_roots
    build(workspace, config)
    state = load_state(workspace)
    assert state["nodes"] == 12 and state["last_build"] > 0


def test_paths_are_rewritten_workspace_relative(tmp_path, monkeypatch, server_graph):
    """The hooks match on the path the agent types; the index must hold it."""
    from codegraph import backends

    root = tmp_path / "server"
    (root / "core").mkdir(parents=True)
    (root / "core" / "book.py").write_text("# real file\n")

    # The backend reports paths relative to the root, not the workspace.
    relative = Graph()
    relative.add_node("book", label="OrderBook", source_file="core/book.py",
                      source_location="L40", file_type="code")
    FakeBackend.graphs = {"server": relative}
    monkeypatch.setitem(backends.REGISTRY, "fake", FakeBackend)

    config = Config(backend="fake", roots=[Root(name="server", path="server")])
    config.save(tmp_path)
    build(tmp_path, config)

    graph = Graph.load(tmp_path / ".codegraph" / "graph.json")
    assert graph.nodes["server::book"]["source_file"] == "server/core/book.py"


def test_unresolvable_paths_are_left_alone(tmp_path, monkeypatch):
    """Better an unrewritten path than one pointing at a file that isn't there."""
    from codegraph import backends

    (tmp_path / "server").mkdir()
    graph = Graph()
    graph.add_node("ghost", label="Ghost", source_file="does/not/exist.py", file_type="code")
    FakeBackend.graphs = {"server": graph}
    monkeypatch.setitem(backends.REGISTRY, "fake", FakeBackend)

    config = Config(backend="fake", roots=[Root(name="server", path="server")])
    config.save(tmp_path)
    build(tmp_path, config)

    built = Graph.load(tmp_path / ".codegraph" / "graph.json")
    assert built.nodes["server::ghost"]["source_file"] == "does/not/exist.py"


def test_a_failing_root_falls_back_to_its_last_good_graph(two_roots, monkeypatch):
    """A broken build should degrade to a stale graph, not to no graph."""
    workspace, config = two_roots
    build(workspace, config)

    def explode(self, path, *, out_dir, incremental=False):
        raise BackendError("backend exploded")

    monkeypatch.setattr(FakeBackend, "extract", explode)
    report = build(workspace, config)

    assert report.errors and "exploded" in report.errors[0]
    assert report.nodes == 12                      # served from cache
    assert report.roots["server"]["stale"] is True


def test_total_backend_failure_raises(tmp_path, monkeypatch):
    from codegraph import backends

    (tmp_path / "server").mkdir()

    def explode(self, path, *, out_dir, incremental=False):
        raise BackendError("nothing works")

    monkeypatch.setattr(FakeBackend, "extract", explode)
    monkeypatch.setitem(backends.REGISTRY, "fake", FakeBackend)
    config = Config(backend="fake", roots=[Root(name="server", path="server")])
    config.save(tmp_path)

    with pytest.raises(BackendError, match="every root failed"):
        build(tmp_path, config)


def test_building_one_root_reuses_the_others(two_roots):
    workspace, config = two_roots
    build(workspace, config)
    report = build(workspace, config, roots=["server"])

    assert report.roots["client"].get("reused") is True
    assert report.nodes == 12          # the client's nodes are still there


def test_missing_root_directory_is_an_error_not_a_crash(tmp_path, monkeypatch):
    from codegraph import backends

    monkeypatch.setitem(backends.REGISTRY, "fake", FakeBackend)
    (tmp_path / "server").mkdir()
    FakeBackend.graphs = {"server": Graph()}
    config = Config(
        backend="fake",
        roots=[Root(name="server", path="server"), Root(name="ghost", path="nope")],
    )
    config.save(tmp_path)
    report = build(tmp_path, config)
    assert any("not a directory" in error for error in report.errors)


def test_build_with_no_roots_is_refused(tmp_path):
    config = Config(backend="fake", roots=[])
    config.save(tmp_path)
    with pytest.raises(ValueError, match="no roots"):
        build(tmp_path, config)


def test_annotations_are_reapplied_by_build(two_roots, tmp_path):
    import time

    from codegraph.annotate import Annotation, AnnotationStore

    workspace, config = two_roots
    store = AnnotationStore(workspace / ".codegraph")
    store.append(Annotation(id="ann_1", kind="note", target="server/core/book.py",
                            text="asserted before the build", created_at=time.time()))
    report = build(workspace, config)

    assert report.annotations["applied"] == 1
    graph = Graph.load(report.graph_path)
    assert any(n.get("_origin") == "codegraph-annotation" for n in graph.nodes.values())
