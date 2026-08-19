"""The MCP server: JSON-RPC protocol conformance and tool behaviour.

Driven through the real ``Server`` object with a fake stdout, so the framing and
error shapes under test are the ones a client will actually receive.
"""

from __future__ import annotations

import io
import json

import pytest

from codegraph.build import build
from codegraph.server import HANDLERS, TOOLS, Server, Workspace


class Client:
    """Drives a Server the way an MCP client would."""

    def __init__(self, workspace_path):
        self.out = io.StringIO()
        self.server = Server(Workspace(workspace_path), stdout=self.out)
        self._id = 0

    def request(self, method, params=None):
        self._id += 1
        self.out.seek(0)
        self.out.truncate()
        self.server.handle(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        )
        raw = self.out.getvalue().strip()
        return json.loads(raw) if raw else None

    def call(self, tool_name, /, **arguments):
        # Positional-only: several tools take an argument literally called
        # "name", which would otherwise collide with this parameter.
        response = self.request("tools/call", {"name": tool_name, "arguments": arguments})
        result = response["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return {"__error__": text}
        return json.loads(text)


@pytest.fixture
def client(two_roots):
    workspace, config = two_roots
    build(workspace, config)
    return Client(workspace)


# ------------------------------------------------------------------ protocol
def test_initialize_reports_server_info(client):
    result = client.request("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert result["serverInfo"]["name"] == "codegraph"
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"]
    assert "context_for_paths" in result["instructions"]


def test_unknown_protocol_falls_back_to_a_supported_one(client):
    result = client.request("initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert result["protocolVersion"] in ("2025-06-18", "2025-03-26", "2024-11-05")


def test_notifications_get_no_response(client):
    client.out.seek(0), client.out.truncate()
    client.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert client.out.getvalue() == ""


def test_unknown_method_is_a_protocol_error(client):
    response = client.request("does/not/exist")
    assert response["error"]["code"] == -32601


def test_tools_list_is_well_formed(client):
    tools = client.request("tools/list")["result"]["tools"]
    assert len(tools) == len(TOOLS) == len(HANDLERS)
    for tool in tools:
        assert tool["name"] and tool["description"]
        assert tool["inputSchema"]["type"] == "object"
        for name in tool["inputSchema"].get("required", []):
            assert name in tool["inputSchema"]["properties"]


def test_malformed_json_is_reported_not_fatal(two_roots):
    workspace, _config = two_roots
    out = io.StringIO()
    server = Server(Workspace(workspace), stdout=out)
    server.serve_forever(io.StringIO('{"broken\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n'))
    lines = [json.loads(line) for line in out.getvalue().strip().splitlines()]
    assert lines[0]["error"]["code"] == -32700   # parse error
    assert lines[1]["result"] == {}              # and it kept going


# --------------------------------------------------------------------- tools
def test_graph_stats(client):
    stats = client.call("graph_stats")
    assert stats["nodes"] == 12
    assert set(stats["roots"]) == {"server", "client"}
    assert stats["hubs"]


def test_context_for_paths_accepts_a_raw_prompt(client):
    result = client.call("context_for_paths", prompt="fix server/core/book.py")
    assert result["resolved_paths"] == ["server/core/book.py"]
    assert "OrderBook" in result["context"]


def test_context_for_paths_needs_some_input(client):
    assert "__error__" in client.call("context_for_paths")


def test_impact_of_reports_affected_files(client):
    result = client.call("impact_of", target="OrderBook", depth=2)
    assert result["affected_count"] >= 2
    assert any("matching.py" in path for path in result["affected_files"])


def test_shortest_path_labels_edge_origins(client):
    result = client.call("shortest_path", source="OrderService", target="OrderBook")
    assert result["found"] is True
    assert all("origin" in hop for hop in result["hops"])


def test_find_symbol(client):
    result = client.call("find_symbol", name="Order")
    assert result["matches"][0]["label"] == "Order"


def test_get_node_exposes_edge_provenance(client):
    result = client.call("get_node", id="OrderBook")
    assert result["degree"] > 0
    assert all({"relation", "confidence", "origin"} <= set(e) for e in result["edges"])


def test_list_roots_reports_cross_root_edges(client):
    result = client.call("list_roots")
    assert {r["name"] for r in result["roots"]} == {"server", "client"}
    assert result["cross_root_edges"]


def test_tool_errors_are_returned_as_content_not_protocol_errors(client):
    """The model should read the message and try again, not see a transport fault."""
    response = client.request("tools/call", {"name": "get_node", "arguments": {"id": "nope"}})
    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "nothing in the graph matches" in response["result"]["content"][0]["text"]


def test_unknown_tool_is_a_protocol_error(client):
    response = client.request("tools/call", {"name": "no_such_tool", "arguments": {}})
    assert response["error"]["code"] == -32602


# -------------------------------------------------------------- write tools
def test_add_to_graph_persists_and_attaches(client, two_roots):
    workspace, _config = two_roots
    result = client.call("add_to_graph", target="server/core/book.py",
                         note="the heap is pruned lazily")
    assert result["attached"] is True

    listed = client.call("list_annotations")
    assert listed["count"] == 1
    assert listed["annotations"][0]["note"] == "the heap is pruned lazily"
    # Written to the log, so it will survive the next rebuild.
    assert (workspace / ".codegraph" / "annotations.jsonl").exists()


def test_add_to_graph_warns_on_an_unresolved_target(client):
    result = client.call("add_to_graph", target="nowhere/at/all.py", note="orphan")
    assert result["attached"] is False
    assert "does not resolve" in result["warning"]


def test_link_locations_creates_an_asserted_edge(client):
    result = client.call("link_locations", source="TradingClient",
                         target="OrderService", relation="calls_over_http")
    assert result["linked"] is True

    path = client.call("shortest_path", source="TradingClient", target="OrderService")
    assert path["found"] and path["length"] == 1
    assert path["hops"][0]["relation"] == "calls_over_http"


def test_revoke_annotation(client):
    added = client.call("add_to_graph", target="server/core/book.py", note="temporary")
    assert client.call("revoke_annotation", id=added["id"])["revoked"] == added["id"]
    assert client.call("list_annotations")["count"] == 0


def test_revoking_an_unknown_id_errors(client):
    assert "__error__" in client.call("revoke_annotation", id="ann_nope")


# ------------------------------------------------------------------ freshness
def test_the_graph_is_reloaded_when_it_changes_on_disk(client, two_roots):
    """The edit hook rewrites graph.json mid-session; a cached snapshot would lie."""
    workspace, config = two_roots
    assert client.call("graph_stats")["nodes"] == 12

    from codegraph.model import Graph
    graph = Graph.load(workspace / ".codegraph" / "graph.json")
    graph.add_node("server::brand_new", label="BrandNew",
                   source_file="server/new.py", root="server")
    graph.save(workspace / ".codegraph" / "graph.json")

    assert client.call("graph_stats")["nodes"] == 13
    assert client.call("find_symbol", name="BrandNew")["matches"]


def test_a_missing_graph_is_an_actionable_error(tmp_path):
    result = Client(tmp_path).call("graph_stats")
    assert "codegraph build" in result["__error__"]
