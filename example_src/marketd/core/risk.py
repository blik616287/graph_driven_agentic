"""Pre-trade risk.

Runs on the critical path of every order, before anything is reserved or
matched, and answers one question: may this order exist?  The checks are
ordered cheapest-first - a symbol lookup before a balance computation - so the
common rejection costs almost nothing.

Risk returns a decision object rather than raising directly.  Callers that want
an exception call :meth:`RiskEngine.check`; the ones that want to explain a
rejection (a batch validator, a what-if endpoint) inspect the decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import RiskRejected
from ..telemetry.metrics import Registry
from ..util.token_bucket import BucketRegistry
from .models import Instrument, Order, OrderType, Side, TimeInForce
from .money import MONEY_EXP, apply_bps, mul_div, notional_units, pow10


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Per-tier trading limits."""

    max_open_orders: int = 500
    max_orders_per_second: float = 50.0
    order_burst: float = 100.0
    max_notional_units: int = 10_000_000 * 10**MONEY_EXP
    price_band_bps: int = 2_000          # reject quotes >20% away from the mid
    market_order_slippage_bps: int = 500  # headroom reserved for market buys


DEFAULT_LIMITS: dict[str, RiskLimits] = {
    "standard": RiskLimits(),
    "pro": RiskLimits(
        max_open_orders=5_000,
        max_orders_per_second=500.0,
        order_burst=1_000.0,
        max_notional_units=250_000_000 * 10**MONEY_EXP,
        price_band_bps=5_000,
    ),
}


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The verdict, plus the reservation the order will need if approved."""

    ok: bool
    reason: str = ""
    code: str = "risk_rejected"
    reserve_asset: str = ""
    reserve_units: int = 0

    @staticmethod
    def reject(reason: str, code: str = "risk_rejected") -> RiskDecision:
        return RiskDecision(ok=False, reason=reason, code=code)


class RiskEngine:
    """Stateless with respect to orders; stateful only for rate limiting."""

    __slots__ = ("_ledger", "_limits", "_buckets", "_taker_fee_bps", "_m_rejects", "_m_checks")

    def __init__(
        self,
        ledger,
        registry: Registry,
        *,
        limits: dict[str, RiskLimits] | None = None,
        taker_fee_bps: int = 10,
    ) -> None:
        self._ledger = ledger
        self._limits = limits or DEFAULT_LIMITS
        self._taker_fee_bps = taker_fee_bps
        # One bucket registry per engine, keyed by account id.
        default = self._limits["standard"]
        self._buckets = BucketRegistry(default.order_burst, default.max_orders_per_second)
        self._m_rejects = registry.counter("risk_rejections_total", "orders rejected by risk")
        self._m_checks = registry.counter("risk_checks_total", "risk evaluations performed")

    def limits_for(self, tier: str) -> RiskLimits:
        return self._limits.get(tier, self._limits["standard"])

    # --- HOT PATH ---------------------------------------------------------
    def evaluate(
        self,
        order: Order,
        instrument: Instrument,
        *,
        tier: str = "standard",
        open_orders: int,
        reference_price_units: int | None,
        now: float,
        estimated_notional_units: int | None = None,
    ) -> RiskDecision:
        """Approve or reject ``order``, and size its reservation."""
        self._m_checks.inc()
        limits = self.limits_for(tier)

        if not instrument.active:
            return self._rejected(f"{instrument.symbol} is not tradeable")

        if not instrument.valid_qty(order.qty_units):
            return self._rejected(
                f"quantity must be a multiple of {instrument.format_qty(instrument.lot_units)} "
                f"between {instrument.format_qty(instrument.min_qty_units)} and "
                f"{instrument.format_qty(instrument.max_qty_units)}"
            )

        is_market = order.type is OrderType.MARKET
        if is_market:
            if order.tif is TimeInForce.GTC:
                return self._rejected("market orders cannot rest; use ioc or fok")
            if reference_price_units is None:
                return self._rejected("no reference price: market is empty", code="no_liquidity")
            # A market order has no price to reserve against.  The caller is
            # expected to have priced the walk with ``MatchingEngine.estimate``
            # and to pass the result in; the slippage-padded reference price is
            # only a fallback for callers doing a what-if check.
            price_for_reserve = (
                reference_price_units
                + mul_div(reference_price_units, limits.market_order_slippage_bps, 10_000)
                if order.side is Side.BUY
                else reference_price_units
            )
        else:
            if not instrument.valid_price(order.price_units):
                return self._rejected(
                    f"price must be a positive multiple of "
                    f"{instrument.format_price(instrument.tick_units)}"
                )
            band_bps = min(limits.price_band_bps, instrument.price_band_bps)
            if reference_price_units is not None:
                deviation = abs(order.price_units - reference_price_units)
                if deviation * 10_000 > reference_price_units * band_bps:
                    return self._rejected(
                        f"price is more than {band_bps / 100:.2f}% away from the market",
                        code="price_out_of_band",
                    )
            price_for_reserve = order.price_units

        notional = (
            estimated_notional_units
            if estimated_notional_units is not None
            else notional_units(
                price_for_reserve, instrument.price_exp, order.qty_units, instrument.qty_exp
            )
        )
        cap = min(limits.max_notional_units, instrument.max_notional_units)
        if notional > cap:
            return self._rejected(
                f"order notional exceeds the limit of {cap // pow10(MONEY_EXP)} {instrument.quote}",
                code="notional_limit",
            )

        if open_orders >= limits.max_open_orders:
            return self._rejected(
                f"too many open orders (limit {limits.max_open_orders})", code="open_order_limit"
            )

        allowed, _, _ = self._buckets.check(order.account_id, now)
        if not allowed:
            return self._rejected(
                f"order rate exceeds {limits.max_orders_per_second:.0f}/s", code="order_rate_limit"
            )

        # Sizing the reservation is the last step: it is the only one that has
        # to touch the ledger.
        if order.side is Side.BUY:
            asset = instrument.quote
            required = notional + apply_bps(notional, self._taker_fee_bps)
        else:
            asset = instrument.base
            # Quantity units are instrument-scaled; balances are ledger-scaled.
            required = _rescale(order.qty_units, instrument.qty_exp, MONEY_EXP)

        held = self._ledger.available(order.account_id, asset)
        if held < required:
            return RiskDecision.reject(
                f"insufficient {asset}: requires {required} units, {held} available",
                code="insufficient_funds",
            )

        return RiskDecision(ok=True, reserve_asset=asset, reserve_units=required)

    def check(self, order: Order, instrument: Instrument, **kwargs) -> RiskDecision:
        """:meth:`evaluate`, but raise on rejection."""
        decision = self.evaluate(order, instrument, **kwargs)
        if not decision.ok:
            error = RiskRejected(decision.reason, symbol=instrument.symbol)
            error.code = decision.code  # type: ignore[misc] - per-instance override
            raise error
        return decision

    def _rejected(self, reason: str, code: str = "risk_rejected") -> RiskDecision:
        self._m_rejects.inc()
        return RiskDecision.reject(reason, code)


def _rescale(units: int, from_exp: int, to_exp: int) -> int:
    """Move a scaled integer between decimal exponents."""
    if from_exp == to_exp:
        return units
    if to_exp > from_exp:
        return units * pow10(to_exp - from_exp)
    return mul_div(units, 1, pow10(from_exp - to_exp))
