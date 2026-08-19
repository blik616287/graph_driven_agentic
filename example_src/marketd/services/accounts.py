"""Account lifecycle: creation, funding, balances."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..core.ledger import Ledger
from ..core.models import Account, Balance
from ..core.money import MONEY_EXP, to_units
from ..errors import Forbidden, NotFound, ValidationError
from ..storage.memstore import MemoryRepository
from ..telemetry.metrics import Registry

KNOWN_ASSETS = frozenset({"USD", "BTC", "ETH", "SOL"})


class AccountService:
    __slots__ = ("_accounts", "_ledger", "_keys", "_ids", "_clock", "_bus", "_log", "_m_created")

    def __init__(
        self, *, accounts, ledger: Ledger, keys, ids, clock, bus, event_log, registry: Registry
    ) -> None:
        self._accounts: MemoryRepository[Account] = accounts
        self._ledger = ledger
        self._keys = keys
        self._ids = ids
        self._clock = clock
        self._bus = bus
        self._log = event_log
        self._m_created = registry.counter("accounts_created_total", "accounts opened")

    def create(self, name: str, tier: str = "standard") -> tuple[Account, str]:
        """Open an account and mint its first API key.

        The plaintext key is returned to the caller and never stored - this is
        the only moment it exists outside the client.
        """
        account = Account(
            id=self._ids.next_token("acct"),
            name=name,
            tier=tier,
            created_at=self._clock.now(),
        )
        self._accounts.add(account)
        api_key, _record = self._keys.issue(account.id, tier=tier)
        self._m_created.inc()
        self._log.append("account.created", account_id=account.id, tier=tier)
        self._bus.publish_nowait(self._bus.emit("account.created", account_id=account.id))
        return account, api_key

    def get(self, account_id: str) -> Account:
        return self._accounts.require(account_id)

    def authorise(self, principal, account_id: str) -> Account:
        """Fetch an account the caller is allowed to see.

        A caller asking about someone else's account gets a 404, not a 403.  A
        403 confirms the account exists, which is an information leak whenever
        ids are guessable or enumerable.
        """
        if not principal.owns(account_id):
            raise NotFound(f"no account with id {account_id}", id=account_id)
        return self._accounts.require(account_id)

    def deposit(self, account_id: str, asset: str, amount: Decimal) -> dict[str, Any]:
        """Credit an account from outside the venue."""
        account = self._accounts.require(account_id)
        if account.disabled:
            raise Forbidden("account is disabled", account_id=account_id)
        asset = asset.upper()
        if asset not in KNOWN_ASSETS:
            raise ValidationError(
                **{"$.asset": f"unknown asset; expected one of {sorted(KNOWN_ASSETS)}"}
            )

        units = to_units(amount, MONEY_EXP)
        if units <= 0:
            raise ValidationError(**{"$.amount": "must be greater than zero"})

        reference = self._ids.next_token("dep")
        entry = self._ledger.deposit(account_id, asset, units, ref=reference)
        self._log.append(
            "account.deposited", account_id=account_id, asset=asset, units=units, entry_id=entry.id
        )
        return {
            "id": reference,
            "account_id": account_id,
            "asset": asset,
            "amount": str(amount),
            "journal_entry_id": entry.id,
            "balance": self._ledger.balance(account_id, asset).as_dict(),
        }

    def balances(self, account_id: str) -> list[Balance]:
        self._accounts.require(account_id)
        return sorted(self._ledger.balances_for(account_id), key=lambda b: b.asset)
