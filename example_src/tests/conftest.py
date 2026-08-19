"""Shared fixtures.

Every test gets a fresh container with a :class:`ManualClock`, so nothing
depends on wall time and nothing leaks between tests.  ``build_container`` does
no I/O, which is what makes this cheap enough to do per test.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from marketd.api.app import create_app  # noqa: E402
from marketd.bootstrap import build_container  # noqa: E402
from marketd.config import Settings  # noqa: E402
from marketd.util.clock import ManualClock  # noqa: E402


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        port=0,
        api_secret="test-secret",
        rate_limit_rps=10_000.0,
        rate_limit_burst=10_000.0,
        log_level="CRITICAL",
    )


@pytest.fixture
def container(settings, clock):
    built = build_container(settings, clock)
    create_app(built)  # registers routes; harmless if the test never dispatches
    return built


@pytest.fixture
def app(container):
    return create_app(container)


@pytest.fixture
def funded(container):
    """Two funded accounts and their principals.

    Returns ``(maker, taker)`` where each is a ``(account, principal)`` pair.
    """
    from marketd.core.money import MONEY_EXP, to_units

    def make(name: str, tier: str, funds: dict[str, str]):
        account, api_key = container.account_service.create(name, tier)
        for asset, amount in funds.items():
            container.ledger.deposit(account.id, asset, to_units(amount, MONEY_EXP), ref="test")
        principal = container.api_keys.authenticate(f"Bearer {api_key}")
        return account, principal, api_key

    maker = make("Maker", "pro", {"USD": "1000000", "BTC": "50"})
    taker = make("Taker", "standard", {"USD": "1000000", "BTC": "50"})
    return maker, taker


def pytest_pyfunc_call(pyfuncitem):
    """Run ``async def`` tests without a plugin.

    ``pytest-asyncio`` would do this and more, but the whole project is
    stdlib-only and this is six lines: pull the fixtures pytest already
    resolved, and hand the coroutine to ``asyncio.run``.  Each test gets its own
    event loop, which is exactly the isolation you want anyway.
    """
    test_function = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_function):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(test_function(**kwargs))
    return True
