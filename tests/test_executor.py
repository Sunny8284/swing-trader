"""
tests/test_executor.py — order-submission behaviour against a fake broker.

Focus is the exit path. Entries attach a protective stop at the broker while
the profit exit is agent-driven (trailing stop), so an agent-initiated sell has
to retire that stop or it rests against a position we no longer hold.
"""

import pytest

from executor import trade_executor


class FakeOrder:
    def __init__(self, order_id, symbol, order_type="stop"):
        self.id = order_id
        self.symbol = symbol
        self.order_type = order_type


class FakeClient:
    """Records the sequence of broker calls so ordering can be asserted."""

    def __init__(self, open_orders=None, position=True):
        self.calls = []
        self._open_orders = open_orders if open_orders is not None else []
        self._position = position

    def get_open_position(self, ticker):
        self.calls.append(("get_open_position", ticker))
        if not self._position:
            raise Exception("position does not exist")
        return object()

    def get_orders(self, filter=None):
        self.calls.append(("get_orders", None))
        return list(self._open_orders)

    def cancel_order_by_id(self, order_id):
        self.calls.append(("cancel", order_id))

    def close_position(self, ticker):
        self.calls.append(("close_position", ticker))
        return FakeOrder("sell-1", ticker)


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient(open_orders=[FakeOrder("stop-1", "AAPL")])
    monkeypatch.setattr(trade_executor, "_get_client", lambda: client)
    monkeypatch.setattr(trade_executor.storage, "save_trade", lambda **kw: None)
    return client


def test_sell_cancels_resting_stop_before_closing(fake_client):
    """The stop must be cancelled BEFORE the liquidating order is sent."""
    trade_executor.execute_sell(ticker="AAPL", price=150.0)

    names = [c[0] for c in fake_client.calls]
    assert "cancel" in names, "resting stop was never cancelled"
    assert "close_position" in names
    assert names.index("cancel") < names.index("close_position"), (
        "cancelled the stop after liquidating — leaves a window where the stop "
        "can fill against a position that is already gone"
    )


def test_sell_cancels_every_resting_order(monkeypatch):
    client = FakeClient(open_orders=[FakeOrder("a", "COST"), FakeOrder("b", "COST")])
    monkeypatch.setattr(trade_executor, "_get_client", lambda: client)
    monkeypatch.setattr(trade_executor.storage, "save_trade", lambda **kw: None)

    trade_executor.execute_sell(ticker="COST", price=900.0)

    cancelled = {c[1] for c in client.calls if c[0] == "cancel"}
    assert cancelled == {"a", "b"}


def test_no_position_means_no_cancel_and_no_close(monkeypatch):
    client = FakeClient(open_orders=[FakeOrder("stop-1", "LLY")], position=False)
    monkeypatch.setattr(trade_executor, "_get_client", lambda: client)

    result = trade_executor.execute_sell(ticker="LLY", price=100.0)

    assert result is None
    names = [c[0] for c in client.calls]
    assert "close_position" not in names
    assert "cancel" not in names


def test_cancel_survives_a_failing_cancel(monkeypatch):
    """One unfillable cancel must not stop the others."""
    client = FakeClient(open_orders=[FakeOrder("bad", "META"), FakeOrder("good", "META")])

    def flaky(order_id):
        client.calls.append(("cancel", order_id))
        if order_id == "bad":
            raise Exception("order already filled")

    client.cancel_order_by_id = flaky
    monkeypatch.setattr(trade_executor, "_get_client", lambda: client)

    cancelled = trade_executor.cancel_open_orders("META")

    assert cancelled == 1
    assert ("cancel", "good") in client.calls
