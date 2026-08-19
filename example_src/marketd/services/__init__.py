"""Use-case orchestration.

A service owns one workflow end to end and is the only place that knows the
*order* of the steps.  ``OrderService.place`` is the canonical example: validate,
price, risk-check, reserve, match, settle, persist, publish - each step
delegated to a component that knows nothing about the others.

Handlers stay thin (parse, call, present) and domain objects stay pure.  The
coordination has to live somewhere; it lives here, where it can be tested
without a socket.
"""

from .accounts import AccountService
from .market_data import MarketDataService
from .orders import OrderService

__all__ = ["AccountService", "MarketDataService", "OrderService"]
