"""Double-entry ledger.

Every movement of value is a :class:`JournalEntry` whose postings sum to zero
*per asset*.  That invariant is asserted on every write, which turns a whole
category of accounting bugs into a loud failure at the moment of the mistake
rather than a discrepancy discovered at month end.

Money that leaves the system boundary still has a counterparty: deposits are
funded from :data:`EXTERNAL_ACCOUNT` and fees are credited to
:data:`FEE_ACCOUNT`.  Because those are real accounts, "the books balance" is
checkable with :meth:`Ledger.verify` - and the test suite checks it after every
scenario.

Balances have two pockets, ``available`` and ``reserved``.  Placing an order
moves funds into ``reserved``; settling a fill debits *from* reserved.  A
resting order therefore cannot be outspent by a second order, and cancelling
simply hands the reservation back.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..errors import Conflict, InsufficientFunds, NotFound
from ..telemetry.metrics import Registry
from .models import Balance, JournalEntry, Posting

EXTERNAL_ACCOUNT = "sys_external"   # the world outside the venue
FEE_ACCOUNT = "sys_fees"            # the venue's own revenue account
SYSTEM_ACCOUNTS = frozenset({EXTERNAL_ACCOUNT, FEE_ACCOUNT})


class Ledger:
    """Balances plus an append-only journal."""

    __slots__ = ("_balances", "_journal", "_ids", "_clock", "_m_postings", "_m_entries")

    def __init__(self, ids, clock, registry: Registry) -> None:
        self._balances: dict[tuple[str, str], Balance] = {}
        self._journal: list[JournalEntry] = []
        self._ids = ids
        self._clock = clock
        self._m_entries = registry.counter("ledger_entries_total", "journal entries written")
        self._m_postings = registry.counter("ledger_postings_total", "postings written")

    # ------------------------------------------------------------- balances
    def balance(self, account_id: str, asset: str) -> Balance:
        """Fetch or create the (account, asset) balance.

        Auto-vivifying is safe here because a zero balance carries no
        information - and it removes a whole class of "no row yet" branches
        from every caller.
        """
        key = (account_id, asset)
        existing = self._balances.get(key)
        if existing is None:
            existing = Balance(account_id=account_id, asset=asset)
            self._balances[key] = existing
        return existing

    def balances_for(self, account_id: str) -> list[Balance]:
        return [b for (acct, _), b in self._balances.items() if acct == account_id]

    def available(self, account_id: str, asset: str) -> int:
        found = self._balances.get((account_id, asset))
        return found.available_units if found is not None else 0

    # ---------------------------------------------------------- reservations
    def reserve(self, account_id: str, asset: str, units: int, ref: str) -> None:
        """Move ``units`` from available to reserved, or refuse.

        Not a journal entry: total holdings do not change, so the books stay
        balanced without one.  Reservations are a *within-account* concern.
        """
        if units <= 0:
            return
        balance = self.balance(account_id, asset)
        if balance.available_units < units:
            raise InsufficientFunds(
                f"insufficient {asset}: need {units} units, have {balance.available_units}",
                asset=asset,
                required_units=units,
                available_units=balance.available_units,
                ref=ref,
            )
        balance.available_units -= units
        balance.reserved_units += units

    def release(self, account_id: str, asset: str, units: int) -> None:
        """Return an unused reservation to available funds."""
        if units <= 0:
            return
        balance = self.balance(account_id, asset)
        if balance.reserved_units < units:
            raise Conflict(
                "cannot release more than is reserved",
                asset=asset,
                reserved_units=balance.reserved_units,
                requested_units=units,
            )
        balance.reserved_units -= units
        balance.available_units += units

    # -------------------------------------------------------------- posting
    # --- HOT PATH ---------------------------------------------------------
    def post(self, postings: Sequence[Posting], ref: str, memo: str = "") -> JournalEntry:
        """Write one balanced entry.

        Two passes on purpose.  The first validates the whole entry - net zero
        per asset, no balance driven negative - and the second applies it.  A
        posting is never half-applied, so there is no rollback path to get
        wrong.
        """
        if not postings:
            raise ValueError("a journal entry needs at least one posting")

        net: dict[str, int] = {}
        for posting in postings:
            net[posting.asset] = net.get(posting.asset, 0) + posting.delta_units
        unbalanced = {asset: total for asset, total in net.items() if total != 0}
        if unbalanced:
            raise Conflict(
                "journal entry does not balance",
                ref=ref,
                net_by_asset=unbalanced,
            )

        for posting in postings:
            if posting.delta_units >= 0 or posting.account_id in SYSTEM_ACCOUNTS:
                continue  # system accounts are allowed to run negative by design
            balance = self.balance(posting.account_id, posting.asset)
            pocket = balance.reserved_units if posting.from_reserved else balance.available_units
            if pocket + posting.delta_units < 0:
                raise InsufficientFunds(
                    f"posting would overdraw {posting.asset}",
                    account_id=posting.account_id,
                    asset=posting.asset,
                    pocket="reserved" if posting.from_reserved else "available",
                    available_units=pocket,
                    required_units=-posting.delta_units,
                    ref=ref,
                )

        for posting in postings:
            balance = self.balance(posting.account_id, posting.asset)
            if posting.from_reserved:
                balance.reserved_units += posting.delta_units
            else:
                balance.available_units += posting.delta_units

        entry = JournalEntry(
            id=self._ids.next_token("jrn"),
            ts=self._clock.now(),
            ref=ref,
            memo=memo,
            postings=tuple(postings),
        )
        self._journal.append(entry)
        self._m_entries.inc()
        self._m_postings.inc(len(postings))
        return entry

    def deposit(self, account_id: str, asset: str, units: int, ref: str) -> JournalEntry:
        """Fund an account from outside the venue."""
        if units <= 0:
            raise ValueError("deposit must be positive")
        return self.post(
            [
                Posting(EXTERNAL_ACCOUNT, asset, -units, kind="deposit"),
                Posting(account_id, asset, units, kind="deposit"),
            ],
            ref=ref,
            memo=f"deposit {units} {asset}",
        )

    def withdraw(self, account_id: str, asset: str, units: int, ref: str) -> JournalEntry:
        if units <= 0:
            raise ValueError("withdrawal must be positive")
        return self.post(
            [
                Posting(account_id, asset, -units, kind="withdrawal"),
                Posting(EXTERNAL_ACCOUNT, asset, units, kind="withdrawal"),
            ],
            ref=ref,
            memo=f"withdraw {units} {asset}",
        )

    # --- HOT PATH ---------------------------------------------------------
    def settle_fill(
        self,
        *,
        base: str,
        quote: str,
        buyer_id: str,
        seller_id: str,
        qty_units: int,
        notional_units: int,
        buyer_fee_units: int,
        seller_fee_units: int,
        buyer_from_reserved: bool,
        seller_from_reserved: bool,
        ref: str,
    ) -> JournalEntry:
        """Exchange base for quote between two accounts and collect the fee.

        Five postings, and both assets net to zero:

            quote:  -(notional + buyer_fee) + (notional - seller_fee) + fees = 0
            base:   -qty + qty                                                = 0
        """
        postings = [
            Posting(buyer_id, quote, -(notional_units + buyer_fee_units),
                    from_reserved=buyer_from_reserved, kind="trade"),
            Posting(buyer_id, base, qty_units, kind="trade"),
            Posting(seller_id, base, -qty_units,
                    from_reserved=seller_from_reserved, kind="trade"),
            Posting(seller_id, quote, notional_units - seller_fee_units, kind="trade"),
        ]
        fee_total = buyer_fee_units + seller_fee_units
        if fee_total:
            postings.append(Posting(FEE_ACCOUNT, quote, fee_total, kind="fee"))
        return self.post(postings, ref=ref, memo=f"fill {qty_units} {base}")

    # ----------------------------------------------------------- inspection
    def journal(self, limit: int = 50) -> list[JournalEntry]:
        return self._journal[-limit:]

    def entry(self, entry_id: str) -> JournalEntry:
        for candidate in reversed(self._journal):
            if candidate.id == entry_id:
                return candidate
        raise NotFound(f"no journal entry {entry_id}", entry_id=entry_id)

    def verify(self) -> dict[str, int]:
        """Sum every balance per asset.  Must be all zeros.

        Cheap enough to assert in tests after every scenario, and the single
        most valuable check in the system: if this drifts, some code path is
        creating or destroying value.
        """
        totals: dict[str, int] = {}
        for balance in self._balances.values():
            totals[balance.asset] = totals.get(balance.asset, 0) + balance.total_units
        return {asset: total for asset, total in totals.items() if total != 0}

    def totals_by_asset(self) -> dict[str, int]:
        """Value held by *real* accounts, i.e. the venue's liabilities."""
        totals: dict[str, int] = {}
        for (account_id, asset), balance in self._balances.items():
            if account_id in SYSTEM_ACCOUNTS:
                continue
            totals[asset] = totals.get(asset, 0) + balance.total_units
        return totals
