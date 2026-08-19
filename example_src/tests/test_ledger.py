"""Ledger invariants.

Every test ends by asserting the books balance.  That single check catches more
real bugs than any amount of per-case assertion.
"""

from __future__ import annotations

import pytest

from marketd.core.ledger import EXTERNAL_ACCOUNT, FEE_ACCOUNT, Ledger
from marketd.core.models import Posting
from marketd.core.money import MONEY_EXP, to_units
from marketd.errors import Conflict, InsufficientFunds
from marketd.telemetry.metrics import Registry
from marketd.util.idgen import IdGenerator


@pytest.fixture
def ledger(clock):
    return Ledger(IdGenerator(1, clock), clock, Registry())


def units(amount: str) -> int:
    return to_units(amount, MONEY_EXP)


def test_deposit_balances_against_the_external_account(ledger):
    ledger.deposit("alice", "USD", units("100"), ref="d1")
    assert ledger.balance("alice", "USD").available_units == units("100")
    assert ledger.balance(EXTERNAL_ACCOUNT, "USD").available_units == -units("100")
    assert ledger.verify() == {}


def test_unbalanced_entries_are_refused(ledger):
    with pytest.raises(Conflict, match="does not balance"):
        ledger.post(
            [Posting("alice", "USD", units("100")), Posting("bob", "USD", -units("50"))],
            ref="bad",
        )


def test_reserving_moves_funds_without_a_journal_entry(ledger):
    ledger.deposit("alice", "USD", units("100"), ref="d1")
    entries_before = len(ledger.journal(limit=1000))

    ledger.reserve("alice", "USD", units("40"), ref="ord_1")

    balance = ledger.balance("alice", "USD")
    assert (balance.available_units, balance.reserved_units) == (units("60"), units("40"))
    assert balance.total_units == units("100")
    # Total holdings did not change, so there is nothing to journal.
    assert len(ledger.journal(limit=1000)) == entries_before
    assert ledger.verify() == {}


def test_cannot_reserve_more_than_is_available(ledger):
    ledger.deposit("alice", "USD", units("100"), ref="d1")
    ledger.reserve("alice", "USD", units("80"), ref="ord_1")
    with pytest.raises(InsufficientFunds) as caught:
        ledger.reserve("alice", "USD", units("30"), ref="ord_2")
    assert caught.value.details["available_units"] == units("20")


def test_reservations_prevent_double_spending(ledger):
    """The point of the two-pocket balance, stated as a test."""
    ledger.deposit("alice", "USD", units("100"), ref="d1")
    ledger.reserve("alice", "USD", units("100"), ref="order_a")
    with pytest.raises(InsufficientFunds):
        ledger.reserve("alice", "USD", units("100"), ref="order_b")


def test_releasing_more_than_reserved_is_refused(ledger):
    ledger.deposit("alice", "USD", units("100"), ref="d1")
    ledger.reserve("alice", "USD", units("10"), ref="ord_1")
    with pytest.raises(Conflict, match="more than is reserved"):
        ledger.release("alice", "USD", units("50"))


def test_settling_a_fill_moves_both_assets_and_collects_fees(ledger):
    ledger.deposit("buyer", "USD", units("50000"), ref="d1")
    ledger.deposit("seller", "BTC", units("2"), ref="d2")
    notional, buyer_fee, seller_fee = units("30000"), units("30"), units("6")
    ledger.reserve("buyer", "USD", notional + buyer_fee, ref="ord_b")
    ledger.reserve("seller", "BTC", units("1"), ref="ord_s")

    entry = ledger.settle_fill(
        base="BTC", quote="USD", buyer_id="buyer", seller_id="seller",
        qty_units=units("1"), notional_units=notional,
        buyer_fee_units=buyer_fee, seller_fee_units=seller_fee,
        buyer_from_reserved=True, seller_from_reserved=True, ref="trade_1",
    )

    assert entry.net_by_asset() == {"USD": 0, "BTC": 0}
    assert ledger.balance("buyer", "BTC").available_units == units("1")
    # 50000 deposited, 30030 reserved and fully consumed by the fill.
    assert ledger.balance("buyer", "USD").available_units == units("19970")
    assert ledger.balance("buyer", "USD").reserved_units == 0
    assert ledger.balance("seller", "USD").available_units == units("29994")
    assert ledger.balance("seller", "BTC").available_units == units("1")
    assert ledger.balance(FEE_ACCOUNT, "USD").available_units == units("36")
    assert ledger.verify() == {}


def test_a_posting_cannot_overdraw(ledger):
    ledger.deposit("alice", "USD", units("10"), ref="d1")
    with pytest.raises(InsufficientFunds, match="overdraw"):
        ledger.post(
            [Posting("alice", "USD", -units("50")), Posting("bob", "USD", units("50"))],
            ref="overdraft",
        )
    # The whole entry was rejected: validation runs before anything is applied.
    assert ledger.balance("alice", "USD").available_units == units("10")


def test_withdrawal_returns_value_to_the_outside(ledger):
    ledger.deposit("alice", "USD", units("100"), ref="d1")
    ledger.withdraw("alice", "USD", units("40"), ref="w1")
    assert ledger.balance("alice", "USD").available_units == units("60")
    assert ledger.totals_by_asset() == {"USD": units("60")}
    assert ledger.verify() == {}
