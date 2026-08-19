"""Wiring codegraph into coding assistants.

Every assistant ultimately needs the same three things - an MCP server to call,
two hooks, and a paragraph telling the model when to use them - but each spells
them differently. This module owns those spellings.

Two rules apply to every writer here:

*Never clobber.*  Configuration files belong to the user. Existing keys are
merged, and codegraph's own block in a Markdown file is delimited by markers so
it can be rewritten in place without touching anything around it.

*Always absolute.*  The generated config names an interpreter and a directory
by absolute path. An assistant may launch the server from any working
directory, and a relative path that resolves at install time will not resolve
at run time.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Iterator

from . import __version__

MARKER_BEGIN = "<!-- codegraph:begin -->"
MARKER_END = "<!-- codegraph:end -->"

PLATFORMS = ("claude", "cursor", "codex", "agents", "vscode", "gemini")

GUIDANCE = """\
## codegraph

A deterministic AST knowledge graph of this repository is available over MCP.
It is built from tree-sitter parsing only - no model calls - and a hook keeps it
current as files change.

Use it instead of reading whole files to orient yourself:

- `context_for_paths` - before editing a file: what it defines, what depends on
  it, what it depends on, and any recorded notes. Start here.
- `impact_of` - before changing a shared symbol: the blast radius.
- `shortest_path` - how two parts of the system connect.
- `find_symbol` / `get_node` - locate a definition and its edges.
- `add_to_graph` - record a decision or constraint the parser cannot see, so it
  survives for the next session. Do this when the user explains *why*.
- `link_locations` - assert a relationship across repositories.

Edges are labelled with their origin: `ast` was parsed and is fact,
`codegraph-synthetic` was inferred by a heuristic, `codegraph-annotation` was
asserted by a person. Weigh them accordingly.
"""


def _server_command(workspace: Path) -> dict[str, Any]:
    """The MCP stdio server invocation.

    Runs on the *current* interpreter, not the backend's virtualenv: the server
    is stdlib-only by design, and pointing it at the heavy env would make it
    fail wherever that env is missing.
    """
    package_root = str(Path(__file__).resolve().parents[1])
    return {
        "command": sys.executable,
        "args": ["-m", "codegraph", "--workspace", str(workspace), "serve"],
        "env": {"PYTHONPATH": package_root},
    }


def _hook_command(workspace: Path, script: str) -> str:
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    return f'"{sys.executable}" "{scripts_dir / script}" --workspace "{workspace}"'


def _merge_json(path: Path, updates: dict[str, Any]) -> str:
    """Deep-merge ``updates`` into a JSON file, creating it if absent."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            backup = path.with_suffix(path.suffix + ".codegraph-backup")
            shutil.copy2(path, backup)
            return f"!! {path} is not valid JSON; backed up to {backup.name} and skipped"
    merged = _deep_merge(existing, updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return f"ok {path}"


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        elif isinstance(value, list) and isinstance(result.get(key), list):
            # Hook arrays: replace any block we previously wrote, keep the rest.
            others = [item for item in result[key] if not _is_ours(item)]
            result[key] = others + value
        else:
            result[key] = value
    return result


def _is_ours(item: Any) -> bool:
    return isinstance(item, dict) and "codegraph" in json.dumps(item)


def _write_markdown_block(path: Path, body: str) -> str:
    """Insert or replace codegraph's delimited block in a Markdown file."""
    block = f"{MARKER_BEGIN}\n{body.rstrip()}\n{MARKER_END}\n"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if MARKER_BEGIN in text and MARKER_END in text:
            head = text.split(MARKER_BEGIN)[0]
            # Normalise the whitespace around the block so that reinstalling is
            # byte-identical. Without this, each run adds a blank line and the
            # file drifts every time the plugin updates.
            tail = text.split(MARKER_END, 1)[1].lstrip("\n")
            updated = head + block + (f"\n{tail}" if tail else "")
            path.write_text(updated, encoding="utf-8")
            return f"ok {path} (block updated)"
        separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        path.write_text(text + separator + block, encoding="utf-8")
        return f"ok {path} (block appended)"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block, encoding="utf-8")
    return f"ok {path} (created)"


def hooks_config(workspace: Path) -> dict[str, Any]:
    """The two hooks, in Claude Code's settings schema."""
    return {
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(workspace, "codegraph-prompt-hook"),
                        "timeout": 10,
                        "statusMessage": "codegraph: looking up context...",
                    }
                ]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Write|Edit|NotebookEdit|MultiEdit",
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(workspace, "codegraph-edit-hook"),
                        "timeout": 120,
                        # Rebuilding must not stall the agent between tool calls.
                        "async": True,
                        "statusMessage": "codegraph: refreshing graph...",
                    }
                ],
            }
        ],
    }


def install_platform(platform: str, workspace: Path, scope: str = "project") -> Iterator[str]:
    workspace = workspace.resolve()
    server = _server_command(workspace)
    yield f"installing codegraph {__version__} for {platform} ({scope} scope)"

    if platform == "claude":
        yield _merge_json(workspace / ".mcp.json", {"mcpServers": {"codegraph": server}})
        settings = (
            workspace / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")
        )
        yield _merge_json(settings, {"hooks": hooks_config(workspace)})
        yield _write_markdown_block(workspace / "CLAUDE.md", GUIDANCE)
        yield "   note: as a plugin instead, run `/plugin marketplace add <this-repo>` "
        yield "         or symlink codegraph/ into ~/.claude/skills/"

    elif platform == "cursor":
        yield _merge_json(workspace / ".cursor" / "mcp.json", {"mcpServers": {"codegraph": server}})
        rule = workspace / ".cursor" / "rules" / "codegraph.mdc"
        rule.parent.mkdir(parents=True, exist_ok=True)
        rule.write_text(
            "---\ndescription: codegraph knowledge graph\nalwaysApply: true\n---\n\n" + GUIDANCE,
            encoding="utf-8",
        )
        yield f"ok {rule}"

    elif platform in ("codex", "agents"):
        yield _merge_json(workspace / ".mcp.json", {"mcpServers": {"codegraph": server}})
        yield _write_markdown_block(workspace / "AGENTS.md", GUIDANCE)

    elif platform == "gemini":
        yield _merge_json(
            workspace / ".gemini" / "settings.json", {"mcpServers": {"codegraph": server}}
        )
        yield _write_markdown_block(workspace / "GEMINI.md", GUIDANCE)

    elif platform == "vscode":
        # VS Code wraps stdio servers in a "servers" key, not "mcpServers".
        yield _merge_json(
            workspace / ".vscode" / "mcp.json",
            {"servers": {"codegraph": {**server, "type": "stdio"}}},
        )
        yield _write_markdown_block(workspace / ".github" / "copilot-instructions.md", GUIDANCE)

    else:
        yield f"!! unknown platform: {platform}"
        return

    yield "done - restart the assistant (or reload its MCP config) to pick this up"
