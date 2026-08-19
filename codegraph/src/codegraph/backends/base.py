"""The backend contract."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from ..model import Graph


class BackendError(RuntimeError):
    """The backend ran and failed."""


class BackendUnavailable(BackendError):
    """The backend is not installed.  Recoverable by running `make install`."""


class Backend(ABC):
    """Extracts a graph from a directory.

    Implementations must be **deterministic**: same input tree, same graph.  In
    practice that means AST parsing only.  Any backend mode that calls a
    language model belongs behind an explicit flag, never on the path a
    file-edit hook triggers - a hook that costs money per keystroke is a hook
    people turn off.
    """

    name: str = "base"

    def __init__(self, python: str | None = None, timeout: float = 900.0) -> None:
        # Backends live in their own virtualenv (they need tree-sitter and
        # friends); codegraph itself must keep running on a bare Python.
        self.python = python or os.environ.get("CODEGRAPH_BACKEND_PYTHON") or sys.executable
        self.timeout = timeout

    @abstractmethod
    def probe(self) -> str:
        """Return a version/status string, or raise :class:`BackendUnavailable`."""

    @abstractmethod
    def extract(self, path: Path, *, out_dir: Path, incremental: bool = False) -> Graph:
        """Build (or refresh) the graph for ``path`` and return it."""

    # ------------------------------------------------------------- helpers
    def _run(
        self,
        args: list[str],
        cwd: Path | None = None,
        quiet: bool = True,
        env: dict[str, str] | None = None,
    ) -> str:
        try:
            completed = subprocess.run(
                args,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env={**os.environ, **env} if env else None,
            )
        except FileNotFoundError as exc:
            raise BackendUnavailable(f"{args[0]} not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"{self.name} timed out after {self.timeout}s") from exc
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise BackendError(
                f"{self.name} failed (exit {completed.returncode}): "
                + " | ".join(tail[-4:] or ["no output"])
            )
        if not quiet and completed.stderr:
            print(completed.stderr, file=sys.stderr)
        return completed.stdout

    def _module_available(self, module: str) -> bool:
        result = subprocess.run(
            [self.python, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _which(command: str) -> str | None:
        return shutil.which(command)
