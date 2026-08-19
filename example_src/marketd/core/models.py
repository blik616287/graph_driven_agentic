"""Domain entities.

All dataclasses use ``slots=True``.  An ``Order`` is created for every request
and a ``Fill`` for every match, so the per-instance dict a normal class carries
is a measurable cost: slots cut the footprint of these objects roughly in half
and make attribute access a fixed offset instead of a hash lookup.

Value objects (``Instrument``, ``Fill``, ``Trade``, ``Posting``) are frozen.
Only ``Order``, ``Balance`` and ``Account`` mutate, and each carries a
``version`` for optimistic concurrency control.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .money import MONEY_EXP, format_units, pow10


class Side(StrEnum):
    """A ``StrEnum``, not ``class Side(str, Enum)``.

    The difference bites: with the ``str, Enum`` mixin, ``f"{Side.BUY}"`` is
    ``"Side.BUY"`` rather than ``"buy"``, so a value that looks like a string
    everywhere else silently changes shape inside an f-string.
    """

    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(StrEnum):
    GTC = "gtc"  # rest until cancelled
    IOC = "ioc"  # fill what you can, cancel the rest
    FOK = "fok"  # fill entirely, or not at all


class OrderStatus(StrEnum):
    NEW = "new"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradeable pair, plus every constraint the venue enforces on it.

    Exponents are fixed per instrument: BTC-USD quotes to 2 decimals and sizes
    to 8, so its prices live at ``price_exp=2`` and its quantities at
    ``qty_exp=8``.  Storing them here is what lets the engine work in ints.
    """

    symbol: str
    base: str
    quote: str
    price_exp: int
    qty_exp: int
    tick_units: int        # minimum price increment, in price units
    lot_units: int         # minimum quantity increment, in quantity units
    min_qty_units: int
    max_qty_units: int
    max_notional_units: int
    price_band_bps: int = 2_000
    active: bool = True

    def valid_price(self, price_units: int) -> bool:
        return price_units > 0 and price_units % self.tick_units == 0

    def valid_qty(self, qty_units: int) -> bool:
        return (
            self.min_qty_units <= qty_units <= self.max_qty_units
            and qty_units % self.lot_units == 0
        )

    def format_price(self, price_units: int) -> str:
        return format_units(price_units, self.price_exp)

    def format_qty(self, qty_units: int) -> str:
        return format_units(qty_units, self.qty_exp)


@dataclass(slots=True)
class Account:
    id: str
    name: str
    tier: str = "standard"
    created_at: float = 0.0
    disabled: bool = False
    version: int = 1


@dataclass(slots=True)
class Balance:
    """Available versus reserved funds for one (account, asset) pair.

    Reserved funds back resting orders.  Keeping the two apart is what stops an
    account from spending the same dollar on two orders - the classic
    double-spend that a single "balance" column cannot express.
    """

    account_id: str
    asset: str
    available_units: int = 0
    reserved_units: int = 0

    @property
    def total_units(self) -> int:
        return self.available_units + self.reserved_units

    def as_dict(self) -> dict[str, str]:
        return {
            "asset": self.asset,
            "available": format_units(self.available_units, MONEY_EXP),
            "reserved": format_units(self.reserved_units, MONEY_EXP),
            "total": format_units(self.total_units, MONEY_EXP),
        }


@dataclass(slots=True)
class Order:
    id: str
    account_id: str
    symbol: str
    side: Side
    type: OrderType
    tif: TimeInForce
    qty_units: int
    price_units: int = 0            # 0 for market orders
    filled_units: int = 0
    avg_price_units: int = 0        # quantity-weighted, in price units
    status: OrderStatus = OrderStatus.NEW
    client_order_id: str | None = None
    reserved_asset: str = ""
    reserved_units: int = 0         # ledger units still held against this order
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = 1

    @property
    def remaining_units(self) -> int:
        return self.qty_units - self.filled_units

    @property
    def is_open(self) -> bool:
        return not self.status.is_terminal

    def record_fill(self, price_units: int, qty_units: int, now: float) -> None:
        """Apply a fill and roll the volume-weighted average price forward.

        Computed incrementally in integers rather than kept as a running list of
        fills: an order can be hit thousands of times, and the average must not
        drift.
        """
        total = self.filled_units + qty_units
        self.avg_price_units = (
            self.avg_price_units * self.filled_units + price_units * qty_units
        ) // total
        self.filled_units = total
        self.status = (
            OrderStatus.FILLED if total == self.qty_units else OrderStatus.PARTIALLY_FILLED
        )
        self.updated_at = now
        self.version += 1


@dataclass(frozen=True, slots=True)
class Fill:
    """One taker order meeting one maker order.  Always at the maker's price."""

    taker_order_id: str
    maker_order_id: str
    taker_account_id: str
    maker_account_id: str
    symbol: str
    side: Side          # the taker's side
    price_units: int
    qty_units: int


@dataclass(frozen=True, slots=True)
class Trade:
    """A fill, persisted and published.  The public tape is a list of these."""

    id: str
    symbol: str
    price_units: int
    qty_units: int
    taker_side: Side
    buyer_account_id: str
    seller_account_id: str
    buy_order_id: str
    sell_order_id: str
    ts: float


@dataclass(frozen=True, slots=True)
class Posting:
    """One leg of a journal entry.

    ``from_reserved`` selects which pocket of the balance moves.  Debits against
    reserved funds are how a resting buy order pays for itself without ever
    touching the account's spendable balance.
    """

    account_id: str
    asset: str
    delta_units: int
    from_reserved: bool = False
    kind: str = "trade"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A balanced set of postings.  Immutable once written."""

    id: str
    ts: float
    ref: str
    memo: str
    postings: tuple[Posting, ...]

    def net_by_asset(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for posting in self.postings:
            totals[posting.asset] = totals.get(posting.asset, 0) + posting.delta_units
        return totals


@dataclass(slots=True)
class Candle:
    """Rolling OHLCV bucket, maintained by the post-trade worker."""

    symbol: str
    open_time: float
    open_units: int
    high_units: int
    low_units: int
    close_units: int
    volume_units: int = 0
    trades: int = 0

    def update(self, price_units: int, qty_units: int) -> None:
        if price_units > self.high_units:
            self.high_units = price_units
        if price_units < self.low_units:
            self.low_units = price_units
        self.close_units = price_units
        self.volume_units += qty_units
        self.trades += 1


def default_instruments() -> dict[str, Instrument]:
    """The venue's listing.  In a real system this comes from a table."""
    def build(symbol, base, quote, price_exp, qty_exp, tick, lot, min_q, max_q, max_notional):
        return Instrument(
            symbol=symbol,
            base=base,
            quote=quote,
            price_exp=price_exp,
            qty_exp=qty_exp,
            tick_units=tick,
            lot_units=lot,
            min_qty_units=min_q,
            max_qty_units=max_q,
            max_notional_units=max_notional * pow10(MONEY_EXP),
        )

    listings = [
        build("BTC-USD", "BTC", "USD", 2, 8, 50, 1_000, 1_000, 10**11, 25_000_000),
        build("ETH-USD", "ETH", "USD", 2, 8, 5, 10_000, 10_000, 10**12, 10_000_000),
        build("ETH-BTC", "ETH", "BTC", 6, 8, 10, 10_000, 10_000, 10**12, 5_000_000),
        build("SOL-USD", "SOL", "USD", 3, 6, 1, 1_000, 1_000, 10**11, 2_000_000),
    ]
    return {instrument.symbol: instrument for instrument in listings}
