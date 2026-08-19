"""Graph extraction backends.

A backend's whole job is: given a directory, return a :class:`~codegraph.model.Graph`.
Everything downstream - namespacing, merging, synthetic edges, annotations, the
MCP server, the hooks - is backend-agnostic, which is what lets a workspace
switch from graphify to code-review-graph without touching anything else.
"""

from .base import Backend, BackendError, BackendUnavailable
from .graphify import GraphifyBackend
from .crg import CodeReviewGraphBackend

REGISTRY: dict[str, type[Backend]] = {
    GraphifyBackend.name: GraphifyBackend,
    CodeReviewGraphBackend.name: CodeReviewGraphBackend,
}


def get_backend(name: str, **kwargs) -> Backend:
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise BackendError(
            f"unknown backend {name!r}; available: {sorted(REGISTRY)}"
        ) from None
    return cls(**kwargs)


def available_backends(**kwargs) -> dict[str, str]:
    """Map backend name -> status string.  Used by ``codegraph doctor``."""
    status = {}
    for name, cls in REGISTRY.items():
        try:
            status[name] = cls(**kwargs).probe()
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            status[name] = f"error: {exc}"
    return status


__all__ = [
    "Backend", "BackendError", "BackendUnavailable",
    "GraphifyBackend", "CodeReviewGraphBackend",
    "REGISTRY", "get_backend", "available_backends",
]
