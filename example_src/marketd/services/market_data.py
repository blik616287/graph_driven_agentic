"""Public market data: books, tape and tickers.

Read-heavy and hot.  Two caches carry it:

**Book snapshots** are cached for a few tens of milliseconds.  Building one
sorts the live price levels, and under load the same snapshot is requested many
times inside a single tick.  A 50ms TTL bounds staleness to something no human
can perceive while collapsing the work to once per interval.  The cache is
invalidated on every trade anyway, so the TTL only matters for a quiet book.

**The tape** is a bounded deque per symbol.  Recent trades are asked for
constantly and old ones never are, so retention is capped rather than queried.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..core.matching import MatchingEngine
from ..core.models import Candle, Instrument, Trade
from ..core.money import MONEY_EXP, format_units, notional_units
from ..errors import BadRequest, NotFound
from ..telemetry.metrics import Registry
from ..util.lru import LRUCache

MAX_TAPE = 500


class MarketDataService:
    __slots__ = (
        "_instruments", "_engine", "_clock", "_tape", "_candles",
        "_snapshot_cache", "_max_depth", "_m_snapshots",
    )

    def __init__(
        self,
        *,
        instruments: dict[str, Instrument],
        engine: MatchingEngine,
        clock,
        registry: Registry,
        snapshot_ttl: float = 0.05,
        max_depth: int = 50,
    ) -> None:
        self._instruments = instruments
        self._engine = engine
        self._clock = clock
        self._tape: dict[str, deque[Trade]] = {sym: deque(maxlen=MAX_TAPE) for sym in instruments}
        self._candles: dict[str, Candle] = {}
        self._snapshot_cache: LRUCache[tuple[str, int], dict[str, Any]] = LRUCache(
            maxsize=256, ttl=snapshot_ttl
        )
        self._max_depth = max_depth
        self._m_snapshots = registry.counter("book_snapshots_built_total", "book snapshots built")

    def instrument(self, symbol: str) -> Instrument:
        instrument = self._instruments.get(symbol)
        if instrument is None:
            raise NotFound(f"unknown instrument: {symbol}", symbol=symbol)
        return instrument

    def instruments(self) -> list[Instrument]:
        return sorted(self._instruments.values(), key=lambda i: i.symbol)

    # --- HOT PATH ---------------------------------------------------------
    def book(self, symbol: str, depth: int = 10) -> dict[str, Any]:
        self.instrument(symbol)
        if depth < 1 or depth > self._max_depth:
            raise BadRequest(
                f"depth must be between 1 and {self._max_depth}", depth=depth
            )
        key = (symbol, depth)
        now = self._clock.monotonic()
        cached = self._snapshot_cache.get(key, now)
        if cached is not None:
            return cached
        snapshot = self._engine.snapshot(symbol, depth)
        self._m_snapshots.inc()
        self._snapshot_cache.put(key, snapshot, now)
        return snapshot

    def record_trade(self, trade: Trade) -> None:
        """Called by the post-trade worker for every executed trade.

        Invalidating the snapshot cache here rather than on a timer is what
        keeps a moving book from serving stale depth: the cache exists to
        absorb bursts of identical reads, not to delay real changes.
        """
        tape = self._tape.get(trade.symbol)
        if tape is None:
            return
        tape.append(trade)
        for depth in range(1, self._max_depth + 1):
            self._snapshot_cache.invalidate((trade.symbol, depth))
        self._update_candle(trade)

    def trades(self, symbol: str, limit: int = 50) -> list[Trade]:
        self.instrument(symbol)
        tape = self._tape[symbol]
        if limit >= len(tape):
            return list(reversed(tape))
        # Slice from the right: the tape is newest-last and callers want
        # newest-first.
        return list(reversed(list(tape)[-limit:]))

    def ticker(self, symbol: str) -> dict[str, Any]:
        """Last price, best bid/ask and the current candle."""
        instrument = self.instrument(symbol)
        tape = self._tape[symbol]
        last = tape[-1] if tape else None
        snapshot = self.book(symbol, 1)
        candle = self._candles.get(symbol)
        payload: dict[str, Any] = {
            "symbol": symbol,
            "last_price": instrument.format_price(last.price_units) if last else None,
            "last_quantity": instrument.format_qty(last.qty_units) if last else None,
            "last_ts": last.ts if last else None,
            "best_bid": (
                instrument.format_price(snapshot["best_bid_units"])
                if snapshot["best_bid_units"] is not None else None
            ),
            "best_ask": (
                instrument.format_price(snapshot["best_ask_units"])
                if snapshot["best_ask_units"] is not None else None
            ),
            "trades": len(tape),
        }
        if candle is not None:
            payload["candle"] = {
                "open_time": candle.open_time,
                "open": instrument.format_price(candle.open_units),
                "high": instrument.format_price(candle.high_units),
                "low": instrument.format_price(candle.low_units),
                "close": instrument.format_price(candle.close_units),
                "volume": instrument.format_qty(candle.volume_units),
                "trades": candle.trades,
                "notional": format_units(
                    notional_units(
                        candle.close_units, instrument.price_exp,
                        candle.volume_units, instrument.qty_exp,
                    ),
                    MONEY_EXP,
                ),
            }
        return payload

    def candles(self) -> dict[str, Candle]:
        return dict(self._candles)

    def _update_candle(self, trade: Trade, interval: float = 60.0) -> None:
        """Fold a trade into the current bucket, rolling over on the boundary."""
        bucket = trade.ts - (trade.ts % interval)
        candle = self._candles.get(trade.symbol)
        if candle is None or candle.open_time != bucket:
            self._candles[trade.symbol] = Candle(
                symbol=trade.symbol,
                open_time=bucket,
                open_units=trade.price_units,
                high_units=trade.price_units,
                low_units=trade.price_units,
                close_units=trade.price_units,
                volume_units=trade.qty_units,
                trades=1,
            )
        else:
            candle.update(trade.price_units, trade.qty_units)
