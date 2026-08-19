"""Shared fixtures.

Most tests use a fake backend rather than graphify. That is deliberate: the
logic worth testing here - namespacing, merging, synthetic inference, annotation
durability, prompt detection - is backend-agnostic, and binding the suite to a
tree-sitter install would make it slow and environment-dependent. The one test
that does exercise a real backend is marked and skips when it is absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph.backends.base import Backend  # noqa: E402
from codegraph.config import Config, Root  # noqa: E402
from codegraph.model import Graph  # noqa: E402


class FakeBackend(Backend):
    """Returns a canned graph per directory name. Deterministic, instant."""

    name = "fake"
    graphs: dict[str, Graph] = {}

    def probe(self) -> str:
        return "fake ready"

    def extract(self, path: Path, *, out_dir: Path, incremental: bool = False) -> Graph:
        graph = self.graphs.get(path.name)
        if graph is None:
            raise AssertionError(f"no fake graph registered for {path.name!r}")
        # Return a copy: build() mutates what it gets, and a shared instance
        # would leak state between tests.
        return Graph.from_node_link(graph.to_node_link())


def make_graph(entries, edges=(), file_prefix="") -> Graph:
    """``entries`` is (id, label, relative_file, line) tuples."""
    graph = Graph()
    for node_id, label, relative, line in entries:
        graph.add_node(
            node_id,
            label=label,
            source_file=f"{file_prefix}{relative}",
            source_location=f"L{line}",
            file_type="code",
            _callable=not label[0].isupper(),
            _callable_class=label[0].isupper() and not label.endswith(".py"),
        )
    for source, target, relation in edges:
        graph.add_edge(source, target, relation)
    return graph


@pytest.fixture
def workspace(tmp_path) -> Path:
    return tmp_path


@pytest.fixture
def server_graph() -> Graph:
    return make_graph(
        [
            ("models", "models.py", "core/models.py", 1),
            ("order", "Order", "core/models.py", 20),
            ("instrument", "Instrument", "core/models.py", 60),
            ("book", "OrderBook", "core/book.py", 40),
            ("engine", "MatchingEngine", "core/matching.py", 30),
            ("submit", "submit", "core/matching.py", 55),
            ("svc", "OrderService", "services/orders.py", 25),
            ("helper", "order", "tests/test_book.py", 12),
        ],
        [
            ("order", "models", "contains"),
            ("instrument", "models", "contains"),
            ("engine", "book", "calls"),
            ("submit", "book", "calls"),
            ("svc", "engine", "calls"),
            ("svc", "order", "uses"),
        ],
        file_prefix="server/",
    )


@pytest.fixture
def client_graph() -> Graph:
    return make_graph(
        [
            ("cmodels", "models.py", "core/models.py", 1),
            ("corder", "Order", "core/models.py", 15),
            ("cinstrument", "Instrument", "core/models.py", 40),
            ("client", "TradingClient", "trading.py", 10),
        ],
        [("corder", "cmodels", "contains"), ("client", "corder", "uses")],
        file_prefix="client/",
    )


@pytest.fixture
def two_roots(workspace, server_graph, client_graph, monkeypatch):
    """A built two-root workspace, using the fake backend."""
    from codegraph import backends

    (workspace / "server").mkdir()
    (workspace / "client").mkdir()
    FakeBackend.graphs = {"server": server_graph, "client": client_graph}
    monkeypatch.setitem(backends.REGISTRY, "fake", FakeBackend)

    config = Config(
        backend="fake",
        roots=[Root(name="server", path="server"), Root(name="client", path="client")],
    )
    config.save(workspace)
    return workspace, config


def _discover_backend_python() -> str | None:
    """An interpreter with a real extraction backend, or None."""
    import os
    import subprocess

    candidates = [
        os.environ.get("CODEGRAPH_BACKEND_PYTHON"),
        str(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"),
        sys.executable,
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-c", "import graphify"], capture_output=True, check=False
        )
        if probe.returncode == 0:
            return candidate
    return None


@pytest.fixture(scope="session")
def backend_python() -> str:
    found = _discover_backend_python()
    if found is None:
        pytest.skip("no extraction backend installed - run `make install`")
    return found


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: exercises a real extraction backend (needs `make install`)"
    )
