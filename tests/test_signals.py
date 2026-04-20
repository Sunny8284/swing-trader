"""
tests/test_signals.py — Unit tests for the signal generator.

Run with:
    pytest tests/ -v
    pytest tests/ -v --cov=signals --cov-report=term-missing
"""

import numpy as np
import pandas as pd
import pytest

from signals.generator import (
    Signal,
    SignalResult,
    _analyze_ticker,
    _apply_trend_filter,
    generate_signals,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_ohlcv(
    n: int = 250,
    base_price: float = 100.0,
    trend: float = 0.0,
    noise: float = 1.0,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Create a synthetic OHLCV DataFrame for testing.

    Args:
        n:           Number of bars (need ≥ 200 for SMA200 to be defined).
        base_price:  Starting close price.
        trend:       Daily drift added to close price (+ = uptrend, - = downtrend).
        noise:       Standard deviation of random daily moves.
        seed:        RNG seed for reproducibility.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp.today(), periods=n)

    closes = base_price + trend * np.arange(n) + rng.normal(0, noise, n).cumsum()
    closes = np.clip(closes, 1.0, None)  # prices can't be negative

    highs = closes * (1 + rng.uniform(0, 0.02, n))
    lows = closes * (1 - rng.uniform(0, 0.02, n))
    opens = lows + rng.uniform(0, 1, n) * (highs - lows)
    volumes = rng.integers(1_000_000, 10_000_000, n).astype(float)

    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=dates,
    )


@pytest.fixture
def uptrend_df():
    """Strong uptrend — bias toward BUY signals."""
    return _make_ohlcv(n=250, base_price=100.0, trend=0.5, noise=0.5, seed=1)


@pytest.fixture
def downtrend_df():
    """Strong downtrend — bias toward SELL signals."""
    return _make_ohlcv(n=250, base_price=200.0, trend=-0.5, noise=0.5, seed=2)


@pytest.fixture
def flat_df():
    """Flat / choppy market — bias toward HOLD signals."""
    return _make_ohlcv(n=250, base_price=100.0, trend=0.0, noise=0.3, seed=3)


# ── SignalResult structure ─────────────────────────────────────────────────────

class TestAnalyzeTicker:
    def test_returns_signal_result(self, flat_df):
        result = _analyze_ticker("TEST", flat_df)
        assert isinstance(result, SignalResult)

    def test_ticker_name_preserved(self, flat_df):
        result = _analyze_ticker("AAPL", flat_df)
        assert result.ticker == "AAPL"

    def test_signal_is_valid_enum(self, flat_df):
        result = _analyze_ticker("TEST", flat_df)
        assert result.signal in Signal.__members__.values()

    def test_score_in_valid_range(self, flat_df):
        result = _analyze_ticker("TEST", flat_df)
        # Score is sum of 4 sub-indicators, each ±1 or 0
        assert -4 <= result.score <= 4

    def test_price_positive(self, flat_df):
        result = _analyze_ticker("TEST", flat_df)
        assert result.price > 0

    def test_rsi_in_valid_range(self, flat_df):
        result = _analyze_ticker("TEST", flat_df)
        assert 0 <= result.rsi <= 100

    def test_sma20_less_than_sma50_in_downtrend(self, downtrend_df):
        result = _analyze_ticker("TEST", downtrend_df)
        # In a downtrend the short MA should be below the long MA
        assert result.sma20 < result.sma50

    def test_sma20_greater_than_sma50_in_uptrend(self, uptrend_df):
        result = _analyze_ticker("TEST", uptrend_df)
        assert result.sma20 > result.sma50

    def test_bollinger_band_ordering(self, flat_df):
        result = _analyze_ticker("TEST", flat_df)
        assert result.bb_lower < result.bb_upper

    def test_reasons_list_nonempty(self, flat_df):
        result = _analyze_ticker("TEST", flat_df)
        assert len(result.reasons) >= 1

    def test_raises_on_too_few_bars(self):
        tiny_df = _make_ohlcv(n=30)
        with pytest.raises(ValueError, match="Insufficient data"):
            _analyze_ticker("TEST", tiny_df)


# ── Trend filter ───────────────────────────────────────────────────────────────

class TestTrendFilter:
    def test_buy_above_sma200(self):
        """BUY score + price above SMA200 → BUY allowed."""
        signal = _apply_trend_filter(score=3, price=110.0, sma200=100.0, reasons=[])
        assert signal == Signal.BUY

    def test_buy_below_sma200_downgraded(self):
        """BUY score + price below SMA200 → downgrade to HOLD."""
        reasons = []
        signal = _apply_trend_filter(score=3, price=90.0, sma200=100.0, reasons=reasons)
        assert signal == Signal.HOLD
        assert any("downgraded" in r for r in reasons)

    def test_sell_below_sma200(self):
        """SELL score + price below SMA200 → SELL allowed."""
        signal = _apply_trend_filter(score=-3, price=90.0, sma200=100.0, reasons=[])
        assert signal == Signal.SELL

    def test_sell_above_sma200_downgraded(self):
        """SELL score + price above SMA200 → downgrade to HOLD."""
        reasons = []
        signal = _apply_trend_filter(score=-3, price=110.0, sma200=100.0, reasons=reasons)
        assert signal == Signal.HOLD
        assert any("downgraded" in r for r in reasons)

    def test_hold_score_always_hold(self):
        """Mid-range score → HOLD regardless of SMA200."""
        signal = _apply_trend_filter(score=1, price=50.0, sma200=100.0, reasons=[])
        assert signal == Signal.HOLD

    def test_no_sma200_bypass_filter(self):
        """When SMA200 is None (insufficient data) the filter is skipped."""
        signal = _apply_trend_filter(score=3, price=50.0, sma200=None, reasons=[])
        assert signal == Signal.BUY


# ── generate_signals ───────────────────────────────────────────────────────────

class TestGenerateSignals:
    def test_returns_one_result_per_ticker(self, flat_df, uptrend_df):
        data = {"FLAT": flat_df, "UP": uptrend_df}
        results = generate_signals(data)
        assert len(results) == 2
        tickers = {r.ticker for r in results}
        assert tickers == {"FLAT", "UP"}

    def test_skips_invalid_ticker_gracefully(self, flat_df):
        """A bad DataFrame (too short) should be skipped without crashing."""
        tiny_df = _make_ohlcv(n=10)
        data = {"GOOD": flat_df, "BAD": tiny_df}
        results = generate_signals(data)
        # Only GOOD should succeed
        assert len(results) == 1
        assert results[0].ticker == "GOOD"

    def test_empty_input(self):
        results = generate_signals({})
        assert results == []

    def test_all_signals_are_valid(self, uptrend_df, downtrend_df, flat_df):
        data = {"UP": uptrend_df, "DOWN": downtrend_df, "FLAT": flat_df}
        results = generate_signals(data)
        for r in results:
            assert r.signal in (Signal.BUY, Signal.SELL, Signal.HOLD)
