"""
tests/test_cash_sleeve.py — the passive index sleeve.

Idle cash was the bulk of the gap between this account (+2%) and SPY (+16.9%)
over 180 days. The sleeve parks that cash in an ETF. It is passive: the swing
strategy must never trade it, and the trailing stop must never sell it.
"""

import pytest

import config
from agent import trader as trader_mod
from agent.trader import TradingAgent
from executor import trade_executor
from signals.generator import Signal, SignalResult


class Acct:
    def __init__(self, cash):
        self.cash = cash


class Pos:
    def __init__(self, symbol, market_value, price):
        self.symbol = symbol
        self.market_value = market_value
        self.current_price = price


class SleeveClient:
    def __init__(self, cash, held=None):
        self._acct = Acct(cash)
        self._held = held or []
        self.orders = []

    def get_account(self):
        return self._acct

    def get_all_positions(self):
        return list(self._held)

    def submit_order(self, order_data):
        self.orders.append(order_data)
        return type("O", (), {"id": "sleeve-1"})


def test_buys_when_cash_is_idle(monkeypatch):
    client = SleeveClient(cash=50_000)
    monkeypatch.setattr(trade_executor, "_get_client", lambda: client)
    monkeypatch.setattr(trade_executor, "_last_price", lambda t: 500.0)

    result = trade_executor.rebalance_cash_sleeve()

    assert result is not None
    assert len(client.orders) == 1
    order = client.orders[0]
    assert order.symbol == config.PARK_IDLE_CASH_IN
    # 75% of 50k / $500 = 75 shares
    assert order.qty == 75


def test_no_churn_inside_the_drift_band(monkeypatch):
    """Already at target — must not trade. Daily round-trips destroy the gain."""
    held = [Pos(config.PARK_IDLE_CASH_IN, market_value=75_000, price=500.0)]
    client = SleeveClient(cash=25_000, held=held)
    monkeypatch.setattr(trade_executor, "_get_client", lambda: client)

    assert trade_executor.rebalance_cash_sleeve() is None
    assert client.orders == []


def test_strategy_never_trades_the_sleeve_ticker(monkeypatch):
    """SPY is on the watchlist; a BUY signal on it must be ignored."""
    def boom(**kwargs):
        raise AssertionError("strategy traded its own cash sleeve")

    monkeypatch.setattr(trader_mod.trade_executor, "execute_buy", boom)
    sig = SignalResult(ticker=config.PARK_IDLE_CASH_IN, signal=Signal.BUY,
                       score=3, price=760.0)

    action = TradingAgent()._evaluate(sig)

    assert action.action == "SKIPPED"
    assert "sleeve" in action.reason.lower()


def test_trailing_stop_skips_the_sleeve(monkeypatch):
    """The sleeve is a passive core — the exit scan must leave it alone."""
    def boom(**kwargs):
        raise AssertionError("trailing stop sold the cash sleeve")

    monkeypatch.setattr(trader_mod.trade_executor, "get_positions", lambda: [
        {"ticker": config.PARK_IDLE_CASH_IN, "unrealized_plpc": -0.30,
         "current_price": 500.0, "unrealized_pl": -9999.0},
    ])
    monkeypatch.setattr(trader_mod.trade_executor, "execute_sell", boom)

    actions = TradingAgent()._check_exit_conditions()

    assert actions == []
