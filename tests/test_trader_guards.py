"""
tests/test_trader_guards.py — the agent's duplicate-entry guards.

These cover the wiring, not just the helpers: a correct has_pending_buy() is
useless if _evaluate() never calls it. Disabling the call in trader.py must
turn one of these red.
"""

import pytest

from agent import trader as trader_mod
from agent.trader import TradingAgent
from signals.generator import Signal, SignalResult


@pytest.fixture
def buy_signal():
    return SignalResult(ticker="COST", signal=Signal.BUY, score=2, price=915.74)


@pytest.fixture
def no_execution(monkeypatch):
    """Fail loudly if any guard lets an order through."""
    def boom(**kwargs):
        raise AssertionError("execute_buy called despite a guard")
    monkeypatch.setattr(trader_mod.trade_executor, "execute_buy", boom)


def test_skips_when_position_already_held(monkeypatch, buy_signal, no_execution):
    monkeypatch.setattr(trader_mod.trade_executor, "has_position", lambda t: True)
    monkeypatch.setattr(trader_mod.trade_executor, "has_pending_buy", lambda t: False)

    action = TradingAgent()._evaluate(buy_signal)

    assert action.action == "SKIPPED"
    assert "open position" in action.reason


def test_skips_when_an_unfilled_buy_is_pending(monkeypatch, buy_signal, no_execution):
    """The Labor Day case: no position yet, but an order already queued."""
    monkeypatch.setattr(trader_mod.trade_executor, "has_position", lambda t: False)
    monkeypatch.setattr(trader_mod.trade_executor, "has_pending_buy", lambda t: True)

    action = TradingAgent()._evaluate(buy_signal)

    assert action.action == "SKIPPED"
    assert "pending" in action.reason.lower()


def test_buys_when_flat_and_nothing_pending(monkeypatch, buy_signal):
    monkeypatch.setattr(trader_mod.trade_executor, "has_position", lambda t: False)
    monkeypatch.setattr(trader_mod.trade_executor, "has_pending_buy", lambda t: False)
    monkeypatch.setattr(trader_mod.trade_executor, "execute_buy",
                        lambda **kw: {"ticker": "COST", "side": "buy", "order_id": "x"})

    action = TradingAgent()._evaluate(buy_signal)

    assert action.action == "BUY"
