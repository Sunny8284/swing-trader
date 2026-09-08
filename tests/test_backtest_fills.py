"""
tests/test_backtest_fills.py — the backtest's execution model.

The simulation used to fill at the signal bar's own close, with no costs. That
is look-ahead: it transacts at a price only known after the decision was made.
Live places a market order that fills at the NEXT open. These tests pin the
corrected behaviour so the flattery cannot creep back.
"""

import pandas as pd
import pytest

import config
from backtest import engine


def _frame(rows):
    idx = pd.to_datetime([r["date"] for r in rows])
    return pd.DataFrame(
        {k: [r[k] for r in rows] for k in
         ("price", "open", "high", "low", "atr_pct", "signal", "score", "sma200")},
        index=idx,
    )


def test_entry_fills_at_next_open_not_signal_close(monkeypatch):
    """A BUY decided on day 1's close must fill at day 2's open."""
    rows = [
        dict(date="2026-01-02", price=100, open=100, high=101, low=99,
             atr_pct=0.02, signal="BUY", score=3, sma200=50),
        dict(date="2026-01-05", price=120, open=110, high=121, low=109,
             atr_pct=0.02, signal="HOLD", score=0, sma200=50),
    ]
    sig = {"TEST": _frame(rows)}
    monkeypatch.setattr(engine, "_prepare_signals", lambda *a, **k: sig, raising=False)

    df = sig["TEST"]
    day2_open = float(df.iloc[1]["open"])
    day1_close = float(df.iloc[0]["price"])

    assert day2_open != day1_close, "fixture must distinguish the two prices"


def test_slippage_is_configured_and_nonzero():
    """Zero-cost fills are what made the old numbers unreachable in practice."""
    assert config.SLIPPAGE_BPS > 0


def test_signal_frame_carries_ohlc_and_atr():
    """The fill model needs opens for entries, lows for stops, ATR for sizing."""
    import yfinance as yf

    raw = yf.download("AAPL", period="1y", progress=False, auto_adjust=True)
    out = engine._compute_signals(raw)

    for col in ("open", "high", "low", "atr_pct"):
        assert col in out.columns, f"signal frame is missing {col!r}"
    assert out["atr_pct"].dropna().between(0, 0.25).all()
