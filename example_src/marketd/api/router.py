"""A trie router with a lookup cache.

Matching ``/v1/accounts/{account_id}/balances`` against a list of compiled
regexes is O(routes) per request and gets slower every time someone adds an
endpoint.  A segment trie is O(segments) - proportional to the URL, not to the
size of the API.

In front of the trie sits an LRU cache keyed by ``(method, path)``.  Real
traffic concentrates on a small set of paths, so the cache absorbs most of the
work; the trie handles the long tail.

**Only successful resolutions are cached.**  Caching misses would let anyone
flood the cache with junk paths and evict the entries that matter - a cache is
an attack surface whenever its keys come from the network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..errors import MethodNotAllowed, NotFound
from ..http.message import Request, Response
from ..util.lru import LRUCache

Handler = Callable[[Request], Awaitable[Response]]


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    pattern: str
    handler: Handler
    name: str
    param_names: tuple[str, ...]


@dataclass(slots=True)
class _Node:
    """One path segment."""

    static: dict[str, _Node] = field(default_factory=dict)
    param: _Node | None = None
    param_name: str = ""
    routes: dict[str, Route] = field(default_factory=dict)  # method -> route


class Router:
    __slots__ = ("_root", "_cache", "_routes")

    def __init__(self, cache_size: int = 2048) -> None:
        self._root = _Node()
        self._cache: LRUCache[tuple[str, str], tuple[Route, tuple[str, ...]]] = LRUCache(cache_size)
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, name: str | None = None) -> Route:
        """Register a route.  ``{name}`` segments become path parameters."""
        if not pattern.startswith("/"):
            raise ValueError(f"route pattern must start with '/': {pattern!r}")

        node = self._root
        param_names: list[str] = []
        for segment in _segments(pattern):
            if segment.startswith("{") and segment.endswith("}"):
                param_name = segment[1:-1]
                if not param_name:
                    raise ValueError(f"empty path parameter in {pattern!r}")
                if node.param is None:
                    node.param = _Node()
                    node.param_name = param_name
                elif node.param_name != param_name:
                    # Two routes disagreeing on a parameter's name at the same
                    # depth is a bug that would otherwise surface as a handler
                    # reading a key that is not there.
                    raise ValueError(
                        f"conflicting parameter names at the same position: "
                        f"{node.param_name!r} vs {param_name!r} in {pattern!r}"
                    )
                param_names.append(param_name)
                node = node.param
            else:
                child = node.static.get(segment)
                if child is None:
                    child = _Node()
                    node.static[segment] = child
                node = child

        method = method.upper()
        if method in node.routes:
            raise ValueError(f"duplicate route: {method} {pattern}")
        route = Route(
            method=method,
            pattern=pattern,
            handler=handler,
            name=name or f"{method.lower()}_{pattern.strip('/').replace('/', '_') or 'root'}",
            param_names=tuple(param_names),
        )
        node.routes[method] = route
        self._routes.append(route)
        self._cache.clear()  # the trie changed; stale entries are now wrong
        return route

    def get(self, pattern: str, handler: Handler, name: str | None = None) -> Route:
        return self.add("GET", pattern, handler, name)

    def post(self, pattern: str, handler: Handler, name: str | None = None) -> Route:
        return self.add("POST", pattern, handler, name)

    def delete(self, pattern: str, handler: Handler, name: str | None = None) -> Route:
        return self.add("DELETE", pattern, handler, name)

    # --- HOT PATH ---------------------------------------------------------
    def resolve(self, method: str, path: str) -> tuple[Route, dict[str, str]]:
        """Find the route for ``(method, path)``.

        Raises :class:`NotFound` when no pattern matches and
        :class:`MethodNotAllowed` when the path exists under another verb - the
        distinction a client needs to tell "wrong URL" from "wrong verb".
        """
        key = (method, path)
        cached = self._cache.get(key)
        if cached is not None:
            route, values = cached
            # strict=True asserts the invariant that a route's parameter names
            # and its captured values are the same length.  They always are, by
            # construction - which is exactly when a cheap assertion is worth
            # keeping, because a violation would mean the trie is corrupt.
            return route, dict(zip(route.param_names, values, strict=True))

        node = self._root
        values: list[str] = []
        for segment in _segments(path):
            child = node.static.get(segment)
            if child is None:
                # Static wins over dynamic: /v1/orders/open beats /v1/orders/{id}.
                if node.param is None or not segment:
                    raise NotFound(f"no route for {method} {path}", path=path)
                values.append(segment)
                node = node.param
            else:
                node = child

        if not node.routes:
            raise NotFound(f"no route for {method} {path}", path=path)

        route = node.routes.get(method)
        if route is None:
            allowed = ", ".join(sorted(node.routes))
            raise MethodNotAllowed(
                f"{method} is not allowed on {path}", allow=allowed, path=path
            )

        self._cache.put(key, (route, tuple(values)))
        return route, dict(zip(route.param_names, values, strict=True))

    @property
    def routes(self) -> list[Route]:
        return list(self._routes)

    def describe(self) -> list[dict[str, str]]:
        """Machine-readable route table, served at ``GET /``."""
        return [
            {"method": route.method, "path": route.pattern, "name": route.name}
            for route in sorted(self._routes, key=lambda r: (r.pattern, r.method))
        ]

    def cache_stats(self) -> dict[str, float]:
        return self._cache.stats()


def _segments(path: str) -> list[str]:
    """Split a path into non-empty segments.

    Collapsing empty segments makes ``/v1//orders`` and ``/v1/orders/`` resolve
    to the same route, which is what clients expect and what stops trivial
    cache-busting via added slashes.
    """
    return [segment for segment in path.split("/") if segment]
