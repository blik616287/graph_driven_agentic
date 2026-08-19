#!/usr/bin/env python3
"""End-to-end walkthrough of marketd.

Boots the real server on an ephemeral port, drives it with the real SDK over
real sockets, and prints what happened at each step.  Nothing is stubbed - the
only concession to being a demo is that it picks its own port and shuts itself
down.

    python demo.py
"""

from __future__ import annotations

import asyncio
import sys

from marketd.api.app import create_app
from marketd.bootstrap import build_container
from marketd.config import Settings
from marketd.http.server import HTTPServer
from marketd.sdk import MarketClient
from marketd.telemetry.logging import configure_logging

SYMBOL = "BTC-USD"


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "-" * max(len(title), 60))


def money(value: str, width: int = 14) -> str:
    return value.rjust(width)


async def main() -> int:
    configure_logging("WARNING")  # the demo prints its own narration

    settings = Settings(port=0, taker_fee_bps=10, maker_fee_bps=2, log_level="WARNING")
    container = build_container(settings)
    app = create_app(container)
    server = HTTPServer(app, settings, container.registry)

    await container.start_workers()
    await server.start()
    port = server.port
    print(f"marketd {container.version} listening on 127.0.0.1:{port}")

    anonymous = MarketClient("127.0.0.1", port)
    try:
        # ---------------------------------------------------------- accounts
        rule("1. Open accounts")
        maker_account = await anonymous.create_account("Maker Capital", tier="pro")
        taker_account = await anonymous.create_account("Taker Trading")
        for account in (maker_account, taker_account):
            print(f"  {account['id']:<18} {account['name']:<16} tier={account['tier']}")
        print("  api keys are returned exactly once, at creation")

        maker = MarketClient("127.0.0.1", port, maker_account["api_key"])
        taker = MarketClient("127.0.0.1", port, taker_account["api_key"])

        # ------------------------------------------------------------- funds
        rule("2. Fund them")
        await maker.deposit(maker_account["id"], "BTC", "10")
        await maker.deposit(maker_account["id"], "USD", "500000")
        await taker.deposit(taker_account["id"], "USD", "250000")
        await taker.deposit(taker_account["id"], "BTC", "2")
        for label, client, account in (("maker", maker, maker_account), ("taker", taker, taker_account)):
            balances = await client.balances(account["id"])
            summary = "  ".join(f"{b['asset']} {money(b['available'])}" for b in balances)
            print(f"  {label:<6} {summary}")

        # -------------------------------------------------------- resting book
        rule("3. Maker quotes both sides")
        quotes = [
            ("sell", "30150.00", "1.5"), ("sell", "30100.00", "2.0"), ("sell", "30050.00", "1.0"),
            ("buy", "29950.00", "1.0"), ("buy", "29900.00", "2.5"), ("buy", "29850.00", "3.0"),
        ]
        for side, price, quantity in quotes:
            order = await maker.place_order(SYMBOL, side, quantity, price)
            print(f"  {order['id']:<18} {side:<4} {money(order['quantity'], 10)} @ {money(price)}  -> {order['status']}")

        book = await maker.book(SYMBOL, depth=5)
        print(f"\n  best bid {book['best_bid']}   best ask {book['best_ask']}   spread {book['spread']}")

        balances = {b["asset"]: b for b in await maker.balances(maker_account["id"])}
        print(f"  maker BTC reserved: {balances['BTC']['reserved']}   USD reserved: {balances['USD']['reserved']}")
        print("  ^ resting orders hold funds in `reserved`, so they cannot be double-spent")

        # ------------------------------------------------------------ trading
        rule("4. Taker crosses the spread (walks two levels)")
        order = await taker.place_order(SYMBOL, "buy", "2.5", "30100.00")
        print(f"  {order['id']} -> {order['status']}")
        print(f"  filled {order['filled_quantity']} at an average of {order.get('average_price')}")
        print("  price-time priority: 1.0 @ 30050 taken first, then 1.5 @ 30100")

        rule("5. Market sell, immediate-or-cancel")
        order = await taker.place_order(SYMBOL, "sell", "1.5", order_type="market", tif="ioc")
        print(f"  {order['id']} -> {order['status']}  filled {order['filled_quantity']} @ {order.get('average_price')}")

        rule("6. Fill-or-kill that cannot fill")
        # Affordable, but larger than the resting liquidity inside the limit -
        # so it is killed whole rather than partially filled.
        resting = await taker.book(SYMBOL, depth=5)
        print(f"  asks within reach: {resting['asks']}")
        order = await taker.place_order(SYMBOL, "buy", "4", "30200.00", tif="fok")
        print(f"  {order['id']} -> {order['status']}  filled {order['filled_quantity']}"
              f"  (killed whole: the book is too thin)")

        # -------------------------------------------------------- the tape
        await container.post_trade.idle()
        rule("7. Public tape and ticker")
        for trade in (await taker.trades(SYMBOL, limit=6)):
            print(f"  {trade['id']:<18} {money(trade['quantity'], 10)} @ {money(trade['price'])}  taker={trade['taker_side']}")
        ticker = await taker.ticker(SYMBOL)
        candle = ticker.get("candle", {})
        print(f"\n  last {ticker['last_price']}  bid {ticker['best_bid']}  ask {ticker['best_ask']}")
        print(f"  candle o/h/l/c {candle.get('open')}/{candle.get('high')}/{candle.get('low')}/{candle.get('close')}"
              f"  volume {candle.get('volume')}  notional {candle.get('notional')}")

        # ------------------------------------------------------- idempotency
        rule("8. Idempotency: the same client_order_id twice")
        first = await taker.place_order(SYMBOL, "buy", "0.5", "29800.00", client_order_id="my-order-1")
        second = await taker.place_order(SYMBOL, "buy", "0.5", "29800.00", client_order_id="my-order-1")
        print(f"  first  -> {first['id']}")
        print(f"  second -> {second['id']}   same order: {first['id'] == second['id']}")

        rule("9. Cancel returns the reservation")
        before = {b["asset"]: b["available"] for b in await taker.balances(taker_account["id"])}
        cancelled = await taker.cancel_order(first["id"])
        after = {b["asset"]: b["available"] for b in await taker.balances(taker_account["id"])}
        print(f"  {cancelled['id']} -> {cancelled['status']}")
        print(f"  USD available {before['USD']} -> {after['USD']}")

        # -------------------------------------------------------- failure paths
        rule("10. Rejections, each with a machine-readable code")
        cases = [
            ("unknown instrument", lambda: taker.place_order("DOGE-USD", "buy", "1", "1.00")),
            ("price off the tick grid", lambda: taker.place_order(SYMBOL, "buy", "1", "30000.07")),
            ("price outside the band", lambda: taker.place_order(SYMBOL, "buy", "1", "90000.00")),
            ("more than the account holds", lambda: taker.place_order(SYMBOL, "buy", "20", "30000.00")),
            ("over the notional cap", lambda: taker.place_order(SYMBOL, "buy", "500", "30000.00")),
            ("quantity below the lot size", lambda: taker.place_order(SYMBOL, "buy", "0.000001", "30000.00")),
            ("market order marked gtc", lambda: taker.place_order(SYMBOL, "buy", "1", order_type="market", tif="gtc")),
        ]
        for label, call in cases:
            try:
                await call()
                print(f"  {label:<28} -> UNEXPECTEDLY ACCEPTED")
            except Exception as exc:
                code = getattr(exc, "code", type(exc).__name__)
                print(f"  {label:<28} -> {getattr(exc, 'status', '?')} {code}: {exc}")

        rule("11. Authorisation")
        try:
            await maker.get(f"/v1/accounts/{taker_account['id']}/balances")
        except Exception as exc:
            print(f"  reading another account's balances -> {getattr(exc, 'status', '?')} {getattr(exc, 'code', '')}")
            print("  (404, not 403: a 403 would confirm the account exists)")
        try:
            await anonymous.get("/v1/orders")
        except Exception as exc:
            print(f"  no API key                          -> {getattr(exc, 'status', '?')} {getattr(exc, 'code', '')}")

        # ------------------------------------------------------------ books
        await container.post_trade.idle()
        rule("12. The books balance")
        drift = container.ledger.verify()
        print(f"  every asset nets to zero across all accounts: {not drift}  {drift or ''}")
        print("  venue liabilities (what customers are owed):")
        for asset, units in sorted(container.ledger.totals_by_asset().items()):
            from marketd.core.money import MONEY_EXP, format_units
            print(f"    {asset:<5} {money(format_units(units, MONEY_EXP), 20)}")
        fees = container.ledger.balances_for("sys_fees")
        for balance in fees:
            print(f"  fees collected: {balance.as_dict()['total']} {balance.asset}")

        # ---------------------------------------------------------- telemetry
        rule("13. Telemetry")
        snapshot = container.registry.snapshot()
        latency = snapshot["http_request_seconds"][0]
        print(f"  requests served     {int(latency['count'])}")
        print(f"  latency mean/p95    {latency['mean'] * 1000:.3f}ms / {latency['p95'] * 1000:.3f}ms")
        print(f"  route cache         {container.router.cache_stats()['hit_ratio']:.1%} hit ratio")
        print(f"  auth cache          {container.api_keys.cache_stats()['hit_ratio']:.1%} hit ratio")
        print(f"  orders placed       {int(snapshot['orders_placed_total'][0]['value'])}")
        print(f"  fills produced      {int(snapshot['engine_fills_total'][0]['value'])}")
        print(f"  journal entries     {int(snapshot['ledger_entries_total'][0]['value'])}")
        print(f"  event log           {container.event_log.stats()['sequence']} records, "
              f"post-trade lag {container.post_trade.stats()['lag']}")
        print(f"  connection pool     {taker.stats()}")

        print("\n  slowest spans:")
        for span in sorted(container.tracer.recent(50), key=lambda s: -s["duration_ms"])[:5]:
            print(f"    {span['name']:<16} {span['duration_ms']:>7.3f}ms  {span['attributes']}")

        rule("Done")
        print("  try: python bench.py    (hot-path microbenchmarks)")
        print("       python -m pytest tests -q")
        return 0
    finally:
        for client in (anonymous, maker, taker):
            try:
                await client.close()
            except Exception:
                pass
        await server.close()
        await container.stop_workers()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
