"""Workspace configuration.

Everything codegraph knows about a workspace lives in ``.codegraph/config.json``
- which roots to index, which backend to use, and which synthetic-edge rules to
apply.  It is plain JSON on purpose: an agent can read it, a human can diff it,
and it belongs in version control next to the code it describes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIRNAME = ".codegraph"
CONFIG_FILENAME = "config.json"
SCHEMA_VERSION = 1

DEFAULT_EXCLUDES = [
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".codegraph", "graphify-out",
]


@dataclass(slots=True)
class Root:
    """One indexed codebase.

    ``name`` is the id namespace.  Two roots may contain identically named
    symbols - namespacing is what keeps them from colliding into one node and
    silently inventing a relationship that does not exist.
    """

    name: str
    path: str
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    language_hint: str = ""

    def resolve(self, workspace: Path) -> Path:
        candidate = Path(self.path)
        return candidate if candidate.is_absolute() else (workspace / candidate).resolve()


@dataclass(slots=True)
class SyntheticRules:
    """How to link roots that share no import edges.

    ``shared_symbol`` and ``mirrored_path`` are heuristics and are labelled as
    such on every edge they produce.  ``explicit`` entries are assertions made
    by a human or an agent and carry full confidence.
    """

    shared_symbol: bool = True
    mirrored_path: bool = True
    min_symbol_length: int = 5
    max_symbol_fanout: int = 8
    explicit: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class Config:
    version: int = SCHEMA_VERSION
    backend: str = "graphify"
    roots: list[Root] = field(default_factory=list)
    synthetic: SyntheticRules = field(default_factory=SyntheticRules)
    hook_context_budget: int = 1800
    hook_max_paths: int = 6
    rebuild_debounce_seconds: float = 2.0

    # ------------------------------------------------------------------ io
    @staticmethod
    def dir_for(workspace: Path) -> Path:
        return workspace / CONFIG_DIRNAME

    @staticmethod
    def path_for(workspace: Path) -> Path:
        return Config.dir_for(workspace) / CONFIG_FILENAME

    @classmethod
    def load(cls, workspace: Path) -> "Config":
        path = cls.path_for(workspace)
        if not path.exists():
            raise FileNotFoundError(
                f"no codegraph workspace at {workspace} - run `codegraph init` first"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def load_or_default(cls, workspace: Path) -> "Config":
        try:
            return cls.load(workspace)
        except FileNotFoundError:
            return cls()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        version = int(raw.get("version", SCHEMA_VERSION))
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"config schema v{version} is newer than this codegraph (v{SCHEMA_VERSION})"
            )
        synthetic_raw = raw.get("synthetic", {}) or {}
        known = {f for f in SyntheticRules.__slots__}
        synthetic = SyntheticRules(**{k: v for k, v in synthetic_raw.items() if k in known})
        root_fields = {f for f in Root.__slots__}
        roots = [
            Root(**{k: v for k, v in entry.items() if k in root_fields})
            for entry in raw.get("roots", [])
        ]
        return cls(
            version=SCHEMA_VERSION,
            backend=raw.get("backend", "graphify"),
            roots=roots,
            synthetic=synthetic,
            hook_context_budget=int(raw.get("hook_context_budget", 1800)),
            hook_max_paths=int(raw.get("hook_max_paths", 6)),
            rebuild_debounce_seconds=float(raw.get("rebuild_debounce_seconds", 2.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "backend": self.backend,
            "roots": [asdict(root) for root in self.roots],
            "synthetic": asdict(self.synthetic),
            "hook_context_budget": self.hook_context_budget,
            "hook_max_paths": self.hook_max_paths,
            "rebuild_debounce_seconds": self.rebuild_debounce_seconds,
        }

    def save(self, workspace: Path) -> Path:
        directory = Config.dir_for(workspace)
        directory.mkdir(parents=True, exist_ok=True)
        path = Config.path_for(workspace)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path

    # ------------------------------------------------------------- accessors
    def root(self, name: str) -> Root:
        for candidate in self.roots:
            if candidate.name == name:
                return candidate
        raise KeyError(f"no root named {name!r}; known roots: {[r.name for r in self.roots]}")

    def add_root(self, root: Root) -> None:
        if any(existing.name == root.name for existing in self.roots):
            raise ValueError(f"root {root.name!r} already exists")
        self.roots.append(root)

    def root_for_file(self, workspace: Path, file_path: Path) -> Root | None:
        """Which root owns ``file_path``.

        Longest match wins, so a root nested inside another is attributed to the
        more specific one.
        """
        resolved = file_path if file_path.is_absolute() else (workspace / file_path).resolve()
        best: Root | None = None
        best_len = -1
        for root in self.roots:
            root_path = root.resolve(workspace)
            try:
                resolved.relative_to(root_path)
            except ValueError:
                continue
            depth = len(root_path.parts)
            if depth > best_len:
                best, best_len = root, depth
        return best


def find_workspace(start: Path | None = None) -> Path:
    """Walk up from ``start`` looking for a ``.codegraph`` directory.

    Same contract as ``git rev-parse --show-toplevel``: the workspace is wherever
    the marker directory is, so tools work from any subdirectory.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_DIRNAME).is_dir():
            return candidate
    return current
