"""Request handlers.

Every handler does exactly three things: validate input, call one service, and
present the result.  If a handler starts making decisions, that logic belongs in
a service where it can be tested without constructing a request.
"""

from . import accounts, admin, market, orders

__all__ = ["accounts", "admin", "market", "orders"]


def register_all(router, container) -> None:
    """Attach every route.  Called once, at start-up."""
    accounts.register(router, container)
    orders.register(router, container)
    market.register(router, container)
    admin.register(router, container)
