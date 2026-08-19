"""The two hooks, run as real subprocesses with real payloads.

Testing them in-process would miss the properties that matter most: that they
are launched with a bare interpreter, read JSON on stdin, and - above all -
never fail in a way that breaks the user's session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codegraph.build import build
from codegraph.model import Graph

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PROMPT_HOOK = SCRIPTS / "codegraph-prompt-hook"
EDIT_HOOK = SCRIPTS / "codegraph-edit-hook"


def run_hook(script: Path, payload: dict, workspace: Path, env_extra=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)          # the hook must locate its own package
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(script), "--workspace", str(workspace)],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=120,
    )


def hook_output(result: subprocess.CompletedProcess) -> dict:
    return json.loads(result.stdout) if result.stdout.strip() else {}


@pytest.fixture
def built(two_roots):
    workspace, config = two_roots
    build(workspace, config)
    return workspace, config


# ------------------------------------------------------------- prompt hook
def test_prompt_hook_injects_context_for_code_prompts(built):
    workspace, _ = built
    result = run_hook(PROMPT_HOOK, {
        "hook_event_name": "UserPromptSubmit",
        "user_input": "fix the cancel bug in server/core/book.py",
        "cwd": str(workspace),
    }, workspace)

    assert result.returncode == 0
    payload = hook_output(result)
    assert "OrderBook" in payload["additionalContext"]
    assert "codegraph context" in payload["additionalContext"]


def test_prompt_hook_stays_silent_for_unrelated_prompts(built):
    workspace, _ = built
    result = run_hook(PROMPT_HOOK, {
        "hook_event_name": "UserPromptSubmit",
        "user_input": "what is the weather today",
        "cwd": str(workspace),
    }, workspace)

    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("payload", [
    {},                                                    # no fields at all
    {"user_input": ""},                                    # empty prompt
    {"user_input": "hi"},                                  # too short to bother
    {"hook_event_name": "UserPromptSubmit"},               # no prompt key
])
def test_prompt_hook_tolerates_odd_payloads(built, payload):
    workspace, _ = built
    result = run_hook(PROMPT_HOOK, {**payload, "cwd": str(workspace)}, workspace)
    assert result.returncode == 0


def test_prompt_hook_fails_open_without_a_graph(tmp_path):
    """No workspace, no graph, no problem - the prompt must still go through."""
    result = run_hook(PROMPT_HOOK, {
        "user_input": "anything at all about code.py", "cwd": str(tmp_path),
    }, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_prompt_hook_fails_open_on_a_corrupt_index(built):
    workspace, _ = built
    (workspace / ".codegraph" / "index.json").write_text("{ not json")
    result = run_hook(PROMPT_HOOK, {
        "user_input": "look at server/core/book.py", "cwd": str(workspace),
    }, workspace)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_prompt_hook_survives_garbage_on_stdin(built):
    workspace, _ = built
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(PROMPT_HOOK), "--workspace", str(workspace)],
        input="this is not json at all", capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0


def test_prompt_hook_respects_the_context_budget(built):
    workspace, config = built
    config.hook_context_budget = 30
    config.save(workspace)
    result = run_hook(PROMPT_HOOK, {
        "user_input": "look at server/core/book.py and MatchingEngine and OrderService",
        "cwd": str(workspace),
    }, workspace)
    payload = hook_output(result)
    assert len(payload.get("additionalContext", "")) < 900


# --------------------------------------------------------------- edit hook
@pytest.mark.integration
def test_edit_hook_rebuilds_after_a_change(tmp_path, backend_python):
    """The end-to-end promise: edit a file, and the graph knows about it.

    Uses a real backend rather than the fake one. The hook runs in its own
    process, so a monkeypatched in-memory backend would be invisible to it -
    and this is precisely the path where a subprocess-only bug would hide.
    """
    from codegraph.config import Config, Root

    source = tmp_path / "pkg"
    source.mkdir()
    (source / "__init__.py").write_text("")
    (source / "core.py").write_text(
        "class Widget:\n"
        "    def spin(self):\n"
        "        return 1\n"
    )
    config = Config(backend="graphify", roots=[Root(name="pkg", path="pkg")])
    config.save(tmp_path)
    build(tmp_path, config, backend_python=backend_python)

    graph_file = tmp_path / ".codegraph" / "graph.json"
    assert not Graph.load(graph_file).find_symbol("Sprocket")

    # Edit the source, age the graph past the debounce window, fire the hook.
    (source / "core.py").write_text(
        "class Widget:\n"
        "    def spin(self):\n"
        "        return 1\n"
        "\n\n"
        "class Sprocket(Widget):\n"
        "    def spin(self):\n"
        "        return 2\n"
    )
    stale = time.time() - 3600
    os.utime(graph_file, (stale, stale))

    result = run_hook(EDIT_HOOK, {
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(source / "core.py")},
        "cwd": str(tmp_path),
    }, tmp_path, env_extra={"CODEGRAPH_BACKEND_PYTHON": backend_python,
                            "CODEGRAPH_HOOK_DEBUG": "1"})

    assert result.returncode == 0, result.stderr
    rebuilt = Graph.load(graph_file)
    assert rebuilt.find_symbol("Sprocket"), f"hook stderr: {result.stderr}"
    # And the detection index the prompt hook reads was refreshed too.
    index = json.loads((tmp_path / ".codegraph" / "index.json").read_text())
    assert "sprocket" in index["symbols"]


def test_edit_hook_debounces_a_burst(built):
    """A freshly built graph should not be rebuilt again immediately."""
    workspace, _ = built
    graph_file = workspace / ".codegraph" / "graph.json"
    before = graph_file.stat().st_mtime

    result = run_hook(EDIT_HOOK, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(workspace / "server" / "core" / "book.py")},
        "cwd": str(workspace),
    }, workspace, env_extra={"CODEGRAPH_HOOK_DEBUG": "1"})

    assert result.returncode == 0
    assert graph_file.stat().st_mtime == before
    assert "debounced" in result.stderr
    # The edit is remembered for the next run past the window.
    assert json.loads((workspace / ".codegraph" / "pending.json").read_text())["roots"] == ["server"]


def test_edit_hook_ignores_files_outside_every_root(built):
    workspace, _ = built
    result = run_hook(EDIT_HOOK, {
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/unrelated.py"},
        "cwd": str(workspace),
    }, workspace, env_extra={"CODEGRAPH_HOOK_DEBUG": "1"})

    assert result.returncode == 0
    assert "outside every indexed root" in result.stderr


def test_edit_hook_respects_a_held_lock(built):
    workspace, _ = built
    graph_file = workspace / ".codegraph" / "graph.json"
    old = time.time() - 3600
    os.utime(graph_file, (old, old))

    lock = workspace / ".codegraph" / "rebuild.lock"
    lock.write_text(str(os.getpid()))          # a live pid: this process

    result = run_hook(EDIT_HOOK, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(workspace / "server" / "core" / "book.py")},
        "cwd": str(workspace),
    }, workspace, env_extra={"CODEGRAPH_HOOK_DEBUG": "1"})

    assert result.returncode == 0
    assert "holds the lock" in result.stderr
    lock.unlink()


def test_edit_hook_steals_a_lock_from_a_dead_process(built):
    workspace, _ = built
    graph_file = workspace / ".codegraph" / "graph.json"
    old = time.time() - 3600
    os.utime(graph_file, (old, old))

    lock = workspace / ".codegraph" / "rebuild.lock"
    lock.write_text("999999")                  # a pid that cannot be running

    result = run_hook(EDIT_HOOK, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(workspace / "server" / "core" / "book.py")},
        "cwd": str(workspace),
    }, workspace, env_extra={"CODEGRAPH_HOOK_DEBUG": "1"})

    assert result.returncode == 0
    assert "stealing stale lock" in result.stderr
    assert not lock.exists()


def test_edit_hook_reads_multiedit_payloads(built):
    workspace, _ = built
    result = run_hook(EDIT_HOOK, {
        "tool_name": "MultiEdit",
        "tool_input": {"edits": [
            {"file_path": str(workspace / "server" / "core" / "book.py")},
            {"file_path": str(workspace / "client" / "trading.py")},
        ]},
        "cwd": str(workspace),
    }, workspace, env_extra={"CODEGRAPH_HOOK_DEBUG": "1"})

    assert result.returncode == 0
    pending = json.loads((workspace / ".codegraph" / "pending.json").read_text())
    assert set(pending["roots"]) == {"server", "client"}


def test_edit_hook_fails_open_without_a_workspace(tmp_path):
    result = run_hook(EDIT_HOOK, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "x.py")},
        "cwd": str(tmp_path),
    }, tmp_path)
    assert result.returncode == 0
