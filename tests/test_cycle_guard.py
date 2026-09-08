"""
tests/test_cycle_guard.py — the market-calendar gate on the trading cycle.

On 2026-09-07 (Labor Day) the scheduler ran three cycles into a closed market.
Nothing below the calendar check is safe then: quotes are stale, so the same
signal regenerates every run, and orders queue unfilled so the duplicate guards
see no position. The cycle must stop before fetching anything.
"""

import main


def test_closed_market_stops_before_fetching(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("fetched data on a closed market")

    monkeypatch.setattr(main.trade_executor, "is_trading_day", lambda: False)
    monkeypatch.setattr(main.fetcher, "fetch_ohlcv", boom)

    main.run_trading_cycle(watchlist=["COST"])  # must return without raising


def test_open_market_proceeds_to_fetch(monkeypatch):
    called = {}

    def fake_fetch(tickers):
        called["yes"] = True
        return {}          # empty -> cycle aborts right after, which is fine

    monkeypatch.setattr(main.trade_executor, "is_trading_day", lambda: True)
    monkeypatch.setattr(main.fetcher, "fetch_ohlcv", fake_fetch)

    main.run_trading_cycle(watchlist=["COST"])

    assert called.get("yes"), "cycle did not reach the data fetch on a trading day"
