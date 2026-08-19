"""Configuration, the code-review-graph adapter, and assistant wiring."""

from __future__ import annotations

import json

import pytest

from codegraph.backends.crg import _first_json_object, translate
from codegraph.config import Config, Root, SyntheticRules, find_workspace
from codegraph.install import PLATFORMS, hooks_config, install_platform


# ------------------------------------------------------------------- config
def test_config_round_trips(tmp_path):
    config = Config(
        backend="graphify",
        roots=[Root(name="a", path="svc/a"), Root(name="b", path="/abs/b")],
        synthetic=SyntheticRules(min_symbol_length=7, explicit=[{"source": "x", "target": "y"}]),
    )
    config.save(tmp_path)
    loaded = Config.load(tmp_path)

    assert [r.name for r in loaded.roots] == ["a", "b"]
    assert loaded.synthetic.min_symbol_length == 7
    assert loaded.synthetic.explicit == [{"source": "x", "target": "y"}]


def test_missing_config_is_an_actionable_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="codegraph init"):
        Config.load(tmp_path)


def test_unknown_config_keys_are_ignored(tmp_path):
    """Forward compatibility: a newer codegraph's extra keys must not crash this one."""
    directory = tmp_path / ".codegraph"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({
        "version": 1, "backend": "graphify",
        "roots": [{"name": "a", "path": "a", "future_field": 1}],
        "synthetic": {"shared_symbol": False, "future_rule": True},
    }))
    loaded = Config.load(tmp_path)
    assert loaded.roots[0].name == "a"
    assert loaded.synthetic.shared_symbol is False


def test_a_newer_schema_is_refused(tmp_path):
    directory = tmp_path / ".codegraph"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"version": 99, "roots": []}))
    with pytest.raises(ValueError, match="newer than this codegraph"):
        Config.load(tmp_path)


def test_duplicate_root_names_are_refused():
    config = Config(roots=[Root(name="a", path="one")])
    with pytest.raises(ValueError, match="already exists"):
        config.add_root(Root(name="a", path="two"))


def test_root_for_file_prefers_the_most_specific_root(tmp_path):
    config = Config(roots=[Root(name="outer", path="repo"),
                           Root(name="inner", path="repo/vendor/lib")])
    (tmp_path / "repo" / "vendor" / "lib").mkdir(parents=True)
    found = config.root_for_file(tmp_path, tmp_path / "repo" / "vendor" / "lib" / "x.py")
    assert found.name == "inner"


def test_root_for_file_returns_none_outside_every_root(tmp_path):
    config = Config(roots=[Root(name="a", path="repo")])
    (tmp_path / "repo").mkdir()
    assert config.root_for_file(tmp_path, tmp_path / "elsewhere" / "x.py") is None


def test_secrets_are_not_in_config_but_paths_are(tmp_path):
    config = Config(roots=[Root(name="a", path="a")])
    config.save(tmp_path)
    assert "api_key" not in (tmp_path / ".codegraph" / "config.json").read_text().lower()


def test_find_workspace_walks_up(tmp_path):
    (tmp_path / ".codegraph").mkdir()
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_workspace(deep) == tmp_path


def test_find_workspace_falls_back_to_the_start(tmp_path):
    assert find_workspace(tmp_path) == tmp_path.resolve()


# ------------------------------------------------ code-review-graph adapter
def test_crg_export_is_translated_to_the_common_shape():
    graph = translate({
        "nodes": [
            {"id": "n1", "name": "OrderBook", "file": "/repo/core/book.py",
             "line": 40, "type": "class"},
            {"id": "n2", "name": "submit", "file": "/repo/core/matching.py",
             "line": 55, "type": "function"},
        ],
        "edges": [{"source": "n2", "target": "n1", "relationship": "call"}],
    })
    assert graph.nodes["n1"]["label"] == "OrderBook"
    assert graph.nodes["n1"]["source_location"] == "L40"
    assert graph.nodes["n1"]["kind"] == "class"
    # crg's vocabulary is normalised to graphify's.
    assert graph.edges[0]["relation"] == "calls"


@pytest.mark.parametrize("raw,expected", [
    ("call", "calls"), ("import", "imports"), ("extends", "inherits"),
    ("implements", "inherits"), ("references", "uses"),
])
def test_crg_relation_aliases(raw, expected):
    graph = translate({"nodes": [], "edges": [{"source": "a", "target": "b", "relationship": raw}]})
    assert graph.edges[0]["relation"] == expected


def test_crg_paths_are_made_relative(tmp_path):
    graph = translate(
        {"nodes": [{"id": "n", "name": "X", "file": str(tmp_path / "src" / "a.py")}], "edges": []},
        source_root=tmp_path,
    )
    assert graph.nodes["n"]["source_file"] == "src/a.py"


def test_crg_edges_missing_an_endpoint_are_skipped():
    graph = translate({"nodes": [], "edges": [{"source": "a"}, {"target": "b"}]})
    assert graph.edges == []


def test_json_is_recovered_from_noisy_cli_output():
    """CLIs interleave progress lines with their payload."""
    text = 'building graph...\nprogress 50%\n{"nodes": [], "edges": []}\ndone in 2s\n'
    assert _first_json_object(text) == {"nodes": [], "edges": []}


def test_json_scraper_skips_non_json_braces():
    text = 'note: use {braces} freely\n{"nodes": []}\n'
    assert _first_json_object(text) == {"nodes": []}


def test_json_scraper_returns_none_when_there_is_none():
    assert _first_json_object("no payload here") is None


# ------------------------------------------------------------------ install
def test_hooks_config_declares_both_events(tmp_path):
    config = hooks_config(tmp_path)
    assert set(config) == {"UserPromptSubmit", "PostToolUse"}
    assert config["PostToolUse"][0]["matcher"] == "Write|Edit|NotebookEdit|MultiEdit"
    # The rebuild must not stall the agent between tool calls.
    assert config["PostToolUse"][0]["hooks"][0]["async"] is True


def test_install_claude_writes_mcp_hooks_and_guidance(tmp_path):
    lines = list(install_platform("claude", tmp_path))
    assert any("done" in line for line in lines)

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert "codegraph" in mcp["mcpServers"]
    assert "--workspace" in mcp["mcpServers"]["codegraph"]["args"]

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "UserPromptSubmit" in settings["hooks"]

    assert "codegraph:begin" in (tmp_path / "CLAUDE.md").read_text()


def test_install_preserves_existing_config(tmp_path):
    """Config files belong to the user; never clobber them."""
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"their-server": {"command": "keep-me"}}
    }))
    list(install_platform("claude", tmp_path))

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert mcp["mcpServers"]["their-server"]["command"] == "keep-me"
    assert "codegraph" in mcp["mcpServers"]


def test_install_is_idempotent(tmp_path):
    list(install_platform("claude", tmp_path))
    first = (tmp_path / "CLAUDE.md").read_text()
    list(install_platform("claude", tmp_path))
    second = (tmp_path / "CLAUDE.md").read_text()

    assert first == second
    assert second.count("codegraph:begin") == 1

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["UserPromptSubmit"]) == 1   # not duplicated


def test_install_keeps_surrounding_markdown(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My project\n\nSome existing notes.\n")
    list(install_platform("claude", tmp_path))
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "Some existing notes." in text
    assert "codegraph:begin" in text


def test_install_backs_up_unparseable_json(tmp_path):
    (tmp_path / ".mcp.json").write_text("{ not json at all")
    lines = list(install_platform("claude", tmp_path))
    assert any("backed up" in line for line in lines)
    assert (tmp_path / ".mcp.json.codegraph-backup").exists()


@pytest.mark.parametrize("platform", list(PLATFORMS))
def test_every_platform_installs_without_error(tmp_path, platform):
    lines = list(install_platform(platform, tmp_path))
    assert not any(line.startswith("!!") for line in lines), lines


def test_vscode_uses_its_own_server_key(tmp_path):
    list(install_platform("vscode", tmp_path))
    payload = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
    assert "servers" in payload and payload["servers"]["codegraph"]["type"] == "stdio"
