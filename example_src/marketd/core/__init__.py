"""The domain layer: prices, orders, books, matching, the ledger and risk.

Nothing in here imports from ``api`` or ``http``.  The dependency arrow points
inwards only, which is why the matching engine can be exercised in a unit test
with no server, no sockets and no event loop.
"""
