"""Application assembly.

The dispatcher is the innermost layer of the chain: resolve the route, stash the
pattern for the metrics label, call the handler.  Everything else - auth, rate
limiting, tracing, error translation - is middleware wrapped around it, built
once at start-up by :func:`create_app`.
"""

from __future__ import annotations

from ..http.message import Request, Response
from ..util.token_bucket import BucketRegistry
from .handlers import register_all
from .middleware import (
    access_log_middleware,
    auth_middleware,
    build_chain,
    error_middleware,
    metrics_middleware,
    rate_limit_middleware,
    request_id_middleware,
    tracing_middleware,
)
from .router import Router

# Endpoints reachable without a key.  An allow-list, checked by exact path.
PUBLIC_PATHS = frozenset(
    {"/", "/healthz", "/readyz", "/metrics", "/v1/accounts", "/v1/instruments"}
)


def create_app(container):
    """Build the request handler.  Returns ``async (Request) -> Response``."""
    router: Router = container.router
    register_all(router, container)

    async def dispatch(request: Request) -> Response:
        route, params = router.resolve(request.method, request.path)
        request.path_params = params
        # Recorded before the handler runs so that the metrics middleware can
        # label a failed request with its route rather than "unmatched".
        request.ctx["route_pattern"] = route.pattern
        return await route.handler(request)

    buckets = BucketRegistry(
        capacity=container.settings.rate_limit_burst,
        rate=container.settings.rate_limit_rps,
    )

    # Order matters; see marketd.api.middleware.
    middlewares = [
        request_id_middleware(container.ids),
        tracing_middleware(container.tracer),
        access_log_middleware(container.clock),
        error_middleware(),
        metrics_middleware(container.registry),
        auth_middleware(container.api_keys, PUBLIC_PATHS),
        rate_limit_middleware(buckets, container.registry, container.clock),
    ]
    return build_chain(dispatch, middlewares)
