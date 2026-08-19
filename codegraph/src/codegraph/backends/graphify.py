"""graphify backend (https://github.com/Graphify-Labs/graphify).

Uses ``graphify extract --code-only``, which is pure tree-sitter AST parsing:
no API key, no network, no LLM.  That is the mode codegraph cares about, because
it is the only one that is reproducible and free to run from a file-edit hook.

graphify's richer modes (``--mode deep``, semantic doc extraction, community
labelling) do call a model.  They are reachable through ``extra_args`` but are
never used by the automatic paths.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..model import Graph
from .base import Backend, BackendError, BackendUnavailable


class GraphifyBackend(Backend):
    name = "graphify"

    def __init__(self, python: str | None = None, timeout: float = 900.0,
                 extra_args: list[str] | None = None) -> None:
        super().__init__(python=python, timeout=timeout)
        self.extra_args = list(extra_args or [])

    def probe(self) -> str:
        if not self._module_available("graphify"):
            raise BackendUnavailable(
                f"graphify is not importable by {self.python} - run `make install-graphify`"
            )
        # graphify has no --version flag on every release; the help banner is
        # the stable signal that the CLI is wired up.
        out = self._run([self.python, "-m", "graphify", "--help"])
        return "graphify ready" if "Commands:" in out else "graphify present (unrecognised CLI)"

    def graph_path(self, path: Path, out_dir: Path) -> Path:
        return self.out_tree(out_dir) / "graph.json"

    @staticmethod
    def out_tree(out_dir: Path) -> Path:
        """Where graphify keeps its graph, manifest and AST cache.

        Redirected out of the source tree via ``GRAPHIFY_OUT``.  Passing
        ``--out`` alone is not enough: graphify still writes its incremental
        manifest and per-file AST cache to ``<source>/graphify-out/``, which
        drops build artifacts into the user's repository.  ``GRAPHIFY_OUT``
        accepts an absolute path and moves all of it.

        Keeping the cache (rather than using a temp dir) is what makes the
        rebuild incremental - it is how graphify knows which files changed.
        """
        return (out_dir / "graphify-out").resolve()

    def extract(self, path: Path, *, out_dir: Path, incremental: bool = False) -> Graph:
        path = path.resolve()
        if not path.is_dir():
            raise BackendError(f"not a directory: {path}")
        out_dir.mkdir(parents=True, exist_ok=True)
        target = self.graph_path(path, out_dir)

        # `extract` is already incremental: it keeps a manifest and re-parses
        # only files whose content changed ("1 code changed; 67 unchanged").
        # `--force` is what turns it into a full re-scan.
        #
        # The obvious-looking alternative, `graphify update`, is wrong here: it
        # writes its graph to `<path>/graphify-out/` next to the source rather
        # than to `--out`, so it would silently update a different file from the
        # one this backend reads back - and drop a build artifact inside the
        # user's source tree.
        args = [
            self.python, "-m", "graphify", "extract", str(path),
            "--code-only",      # AST only: deterministic, no API key, no network
            "--no-cluster",     # clustering is non-deterministic and unused here
        ]
        if not incremental:
            args.append("--force")
        args.extend(self.extra_args)

        env_note = _llm_env_note()
        try:
            self._run(args, cwd=path, env={"GRAPHIFY_OUT": str(self.out_tree(out_dir))})
        except BackendError as exc:
            raise BackendError(f"{exc}{env_note}") from exc

        if not target.exists():
            raise BackendError(
                f"graphify reported success but produced no graph at {target}"
            )
        return Graph.load(target)


def _llm_env_note() -> str:
    known = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
    keys = [name for name in known if os.environ.get(name)]
    if keys:
        return (
            "\nnote: codegraph always passes --code-only, so the failure is not a "
            "missing API key."
        )
    return ""
