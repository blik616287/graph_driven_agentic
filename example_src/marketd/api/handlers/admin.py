"""Operational endpoints.

``/healthz`` and ``/metrics`` are public so a load balancer and a scraper can
reach them without credentials.  ``/v1/admin/*`` is not: it exposes internal
state and needs the ``admin`` scope.

Health checks come in two flavours and conflating them causes outages.
*Liveness* asks "is this process wedged?" and must never depend on anything
external - a liveness check that fails when a dependency is down will restart
every healthy instance in the fleet.  *Readiness* asks "should traffic come
here?" and may.
"""

from __future__ import annotations

from ...http.message import Request, Response


def register(router, container) -> None:
    registry = container.registry
    ledger = container.ledger

    async def healthz(request: Request) -> Response:
        return Response.json({"status": "ok", "version": container.version})

    async def readyz(request: Request) -> Response:
        drift = ledger.verify()
        ready = not drift
        return Response.json(
            {
                "status": "ready" if ready else "degraded",
                # A ledger that does not balance means stop trading, now.
                "ledger_balanced": ready,
                "drift": {asset: str(units) for asset, units in drift.items()},
                "instruments": len(container.instruments),
            },
            status=200 if ready else 503,
        )

    async def metrics(request: Request) -> Response:
        return Response.text(registry.render_prometheus(), content_type="text/plain; version=0.0.4")

    async def stats(request: Request) -> Response:
        request.ctx["principal"].require("admin")
        return Response.json(
            {
                "books": container.engine.stats(),
                "orders": container.orders_repo.count(),
                "trades": container.trades_repo.count(),
                "accounts": container.accounts_repo.count(),
                "event_log": container.event_log.stats(),
                "post_trade": container.post_trade.stats(),
                "scheduler": container.scheduler.stats(),
                "work_queue": {
                    "depth": container.work_queue.depth,
                    "dead_letters": container.work_queue.dead_letters(),
                },
                "caches": {
                    "routes": container.router.cache_stats(),
                    "auth": container.api_keys.cache_stats(),
                },
                "ledger_totals": {
                    asset: str(units) for asset, units in ledger.totals_by_asset().items()
                },
                "metrics": registry.snapshot(),
            }
        )

    async def traces(request: Request) -> Response:
        request.ctx["principal"].require("admin")
        limit = min(request.int_param("limit", 20), 200)
        return Response.json({"data": container.tracer.recent(limit)})

    async def index(request: Request) -> Response:
        return Response.json(
            {
                "service": "marketd",
                "version": container.version,
                "routes": container.router.describe(),
            }
        )

    router.get("/", index, name="index")
    router.get("/healthz", healthz, name="healthz")
    router.get("/readyz", readyz, name="readyz")
    router.get("/metrics", metrics, name="metrics")
    router.get("/v1/admin/stats", stats, name="admin_stats")
    router.get("/v1/admin/traces", traces, name="admin_traces")
