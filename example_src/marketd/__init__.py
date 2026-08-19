"""marketd - a miniature trading venue, written to be read.

The package is deliberately layered so that the call graph is easy to follow
top-down:

    http/       transport: HTTP/1.1 parsing, server loop, client connections
    api/        routing, middleware, auth, request schemas, handlers
    services/   use-case orchestration (place order, deposit, snapshot book)
    core/       the domain: matching engine, order book, ledger, risk
    storage/    repositories, indexes, query builder, append-only event log
    workers/    background consumers driven by the event log
    telemetry/  metrics, tracing, structured logging
    util/       small, dependency-free primitives

Nothing outside the standard library is required.  Functions that run once per
request, per order, or per fill are marked with a ``HOT PATH`` banner - those
are the ones worth profiling before optimising anything else.
"""

from __future__ import annotations

__version__ = "1.4.0"
__all__ = ["__version__"]
