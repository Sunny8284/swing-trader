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


# ── Duplicate-order and market-calendar guards ────────────────────────────────
#
# Regression cover for 2026-09-07 (Labor Day): the scheduler fired three cycles
# into a closed market, stale quotes regenerated the same COST BUY each time,
# and because the orders queued unfilled there was never a position to block the
# next one. Three orders filled at the following open — a 24% position against
# an 8% target.

from datetime import date

from alpaca.trading.enums import OrderSide


class FakeSession:
    def __init__(self, d):
        self.date = d


class CalendarClient:
    def __init__(self, sessions=None, raises=False):
        self._sessions = sessions or []
        self._raises = raises

    def get_calendar(self, req):
        if self._raises:
            raise Exception("calendar unavailable")
        return list(self._sessions)


def test_open_session_is_a_trading_day(monkeypatch):
    day = date(2026, 9, 8)
    monkeypatch.setattr(trade_executor, "_get_client",
                        lambda: CalendarClient([FakeSession(day)]))
    assert trade_executor.is_trading_day(day) is True


def test_holiday_is_not_a_trading_day(monkeypatch):
    """Labor Day is a Monday — a weekday check alone would have passed it."""
    monkeypatch.setattr(trade_executor, "_get_client", lambda: CalendarClient([]))
    assert trade_executor.is_trading_day(date(2026, 9, 7)) is False


def test_calendar_failure_fails_closed(monkeypatch):
    """Unreachable calendar must skip the cycle, not trade blind."""
    monkeypatch.setattr(trade_executor, "_get_client",
                        lambda: CalendarClient(raises=True))
    assert trade_executor.is_trading_day(date(2026, 9, 8)) is False


class OrderSideClient(FakeClient):
    def get_orders(self, filter=None):
        self.calls.append(("get_orders", None))
        return list(self._open_orders)


def _order(oid, symbol, side):
    o = FakeOrder(oid, symbol)
    o.side = side
    return o


def test_pending_buy_blocks_a_duplicate(monkeypatch):
    client = OrderSideClient(open_orders=[_order("buy-1", "COST", OrderSide.BUY)])
    monkeypatch.setattr(trade_executor, "_get_client", lambda: client)
    assert trade_executor.has_pending_buy("COST") is True


def test_resting_stop_does_not_block_reentry(monkeypatch):
    """Entries attach a protective stop; that SELL leg must not look like a
    pending entry, or we could never re-enter a name we still hold a stop on."""
    client = OrderSideClient(open_orders=[_order("stop-1", "COST", OrderSide.SELL)])
    monkeypatch.setattr(trade_executor, "_get_client", lambda: client)
    assert trade_executor.has_pending_buy("COST") is False


def test_pending_check_fails_closed(monkeypatch):
    """If we cannot tell, assume an order exists rather than stacking another."""
    class Boom(FakeClient):
        def get_orders(self, filter=None):
            raise Exception("api down")

    monkeypatch.setattr(trade_executor, "_get_client", lambda: Boom())
    assert trade_executor.has_pending_buy("COST") is True


# ── Volatility-scaled stops ───────────────────────────────────────────────────

def test_stop_falls_back_to_flat_when_atr_unavailable(monkeypatch):
    def boom(*a, **kw):
        raise Exception("yfinance down")
    monkeypatch.setattr(trade_executor.yf, "download", boom)

    import config
    stop = trade_executor.stop_price_for("AAPL", 100.0)
    expected_pct = max(config.ATR_STOP_MIN_PCT, config.STOP_LOSS_PCT)

    assert stop == round(100.0 * (1 - expected_pct), 2)


def test_stop_is_clamped_to_sane_bounds(monkeypatch):
    """A data glitch must not produce a 90% stop."""
    import config
    import pandas as pd

    idx = pd.date_range("2026-01-01", periods=60, freq="D")
    insane = pd.DataFrame({
        "High":  [200.0] * 60,
        "Low":   [1.0] * 60,
        "Close": [100.0] * 60,
    }, index=idx)
    monkeypatch.setattr(trade_executor.yf, "download", lambda *a, **kw: insane)

    stop = trade_executor.stop_price_for("AAPL", 100.0)

    assert stop == round(100.0 * (1 - config.ATR_STOP_MAX_PCT), 2)


def test_more_volatile_name_gets_a_wider_stop(monkeypatch):
    import pandas as pd

    def frame(daily_range):
        idx = pd.date_range("2026-01-01", periods=60, freq="D")
        return pd.DataFrame({
            "High":  [100.0 + daily_range / 2] * 60,
            "Low":   [100.0 - daily_range / 2] * 60,
            "Close": [100.0] * 60,
        }, index=idx)

    monkeypatch.setattr(trade_executor.yf, "download", lambda *a, **kw: frame(2.0))
    calm = trade_executor.stop_price_for("COST", 100.0)

    monkeypatch.setattr(trade_executor.yf, "download", lambda *a, **kw: frame(6.0))
    wild = trade_executor.stop_price_for("NVDA", 100.0)

    assert wild < calm, "the more volatile name must get more room, not less"
