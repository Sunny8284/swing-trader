"""
signals/generator.py — Swing trading signal generation.

Strategy overview
─────────────────
Swing trading captures multi-day to multi-week price moves. We combine
four independent indicators into a composite score:

    +1 per bullish sub-signal, -1 per bearish sub-signal.

    Score ≥  SIGNAL_BUY_THRESHOLD  → BUY
    Score ≤  SIGNAL_SELL_THRESHOLD → SELL
    Otherwise                      → HOLD

Sub-signals used
────────────────
1. RSI (14-period)
   • RSI < 35  → oversold  → +1 (mean-reversion buy)
   • RSI > 65  → overbought → -1 (mean-reversion sell)

2. MACD (12/26/9)
   • MACD line crosses above signal line → +1 (momentum building)
   • MACD line crosses below signal line → -1 (momentum fading)

3. Moving-Average crossover (SMA 20 vs SMA 50)
   • SMA20 > SMA50 (golden cross region) → +1
   • SMA20 < SMA50 (death cross region)  → -1

4. Bollinger Band price position (20-period, 2 std)
   • Close below lower band → +1 (statistically cheap)
   • Close above upper band → -1 (statistically expensive)

   A 200-day SMA trend filter is applied: BUY signals are only issued
   when the price is above SMA200 (uptrend), and SELL signals only when
   it is below SMA200 (downtrend). This avoids catching falling knives.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional

import pandas as pd
import ta
import yfinance as yf

import config

logger = logging.getLogger(__name__)


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class SignalResult:
    ticker: str
    signal: Signal
    score: int                       # composite score (-4 to +4)
    price: float                     # latest close price
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal_line: Optional[float] = None
    sma20: Optional[float] = None
    sma50: Optional[float] = None
    sma200: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    reasons: list[str] = field(default_factory=list)


def generate_signals(data: dict[str, pd.DataFrame]) -> list[SignalResult]:
    """
    Generate BUY/SELL/HOLD signals for all tickers in `data`.

    Args:
        data: Dict of ticker → OHLCV DataFrame (from data/fetcher.py).

    Returns:
        List of SignalResult objects, one per ticker.
    """
    results = []
    for ticker, df in data.items():
        try:
            result = _analyze_ticker(ticker, df)
            results.append(result)
            logger.info(
                "%s → %s (score=%+d, price=%.2f, RSI=%.1f)",
                ticker, result.signal.value, result.score,
                result.price, result.rsi or 0,
            )
        except Exception as exc:
            logger.error("Signal generation failed for %s: %s", ticker, exc)

    return results


def _analyze_ticker(ticker: str, df: pd.DataFrame) -> SignalResult:
    """Compute all indicators and produce a composite signal for one ticker."""

    # Need at least 200 bars for SMA200; warn if fewer
    if len(df) < 50:
        raise ValueError(f"Insufficient data for {ticker} ({len(df)} bars)")

    # Squeeze to 1D Series — newer yfinance can return shape (N,1) DataFrames
    close = df["Close"].squeeze()
    high  = df["High"].squeeze()
    low   = df["Low"].squeeze()

    # ── 1. RSI ─────────────────────────────────────────────────────────────────
    rsi_series = ta.momentum.RSIIndicator(
        close=close, window=config.RSI_PERIOD
    ).rsi()
    rsi = float(rsi_series.iloc[-1])

    # ── 2. MACD ────────────────────────────────────────────────────────────────
    macd_obj = ta.trend.MACD(
        close=close,
        window_fast=config.MACD_FAST,
        window_slow=config.MACD_SLOW,
        window_sign=config.MACD_SIGNAL,
    )
    macd_line = float(macd_obj.macd().iloc[-1])
    signal_line = float(macd_obj.macd_signal().iloc[-1])
    # Yesterday's values for crossover detection
    macd_prev = float(macd_obj.macd().iloc[-2])
    signal_prev = float(macd_obj.macd_signal().iloc[-2])

    # ── 3. Simple Moving Averages ───────────────────────────────────────────────
    sma20 = float(ta.trend.SMAIndicator(close=close, window=config.SMA_SHORT).sma_indicator().iloc[-1])
    sma50 = float(ta.trend.SMAIndicator(close=close, window=config.SMA_LONG).sma_indicator().iloc[-1])
    # SMA200 may not exist if we have fewer than 200 bars; fall back to None
    sma200: Optional[float] = None
    if len(df) >= config.SMA_TREND:
        sma200 = float(ta.trend.SMAIndicator(close=close, window=config.SMA_TREND).sma_indicator().iloc[-1])

    # ── 4. Bollinger Bands ─────────────────────────────────────────────────────
    bb = ta.volatility.BollingerBands(
        close=close,
        window=config.BOLLINGER_PERIOD,
        window_dev=config.BOLLINGER_STD,
    )
    bb_upper = float(bb.bollinger_hband().iloc[-1])
    bb_lower = float(bb.bollinger_lband().iloc[-1])

    price = float(close.iloc[-1])

    # ── Composite Scoring ──────────────────────────────────────────────────────
    score = 0
    reasons: list[str] = []

    # RSI
    if rsi < config.RSI_OVERSOLD:
        score += 1
        reasons.append(f"RSI={rsi:.1f} (oversold < {config.RSI_OVERSOLD})")
    elif rsi > config.RSI_OVERBOUGHT:
        score -= 1
        reasons.append(f"RSI={rsi:.1f} (overbought > {config.RSI_OVERBOUGHT})")

    # MACD crossover (bullish when MACD crosses above signal line)
    if macd_prev <= signal_prev and macd_line > signal_line:
        score += 1
        reasons.append(f"MACD bullish crossover ({macd_line:.4f} > {signal_line:.4f})")
    elif macd_prev >= signal_prev and macd_line < signal_line:
        score -= 1
        reasons.append(f"MACD bearish crossover ({macd_line:.4f} < {signal_line:.4f})")

    # SMA crossover regime
    if sma20 > sma50:
        score += 1
        reasons.append(f"SMA20={sma20:.2f} > SMA50={sma50:.2f} (bullish regime)")
    else:
        score -= 1
        reasons.append(f"SMA20={sma20:.2f} < SMA50={sma50:.2f} (bearish regime)")

    # Bollinger Band position
    if price < bb_lower:
        score += 1
        reasons.append(f"Price {price:.2f} below BB lower {bb_lower:.2f}")
    elif price > bb_upper:
        score -= 1
        reasons.append(f"Price {price:.2f} above BB upper {bb_upper:.2f}")

    # ── Earnings Guard ─────────────────────────────────────────────────────────
    near_earnings = _has_earnings_soon(ticker)

    # ── Volume Confirmation ────────────────────────────────────────────────────
    volume_ok = True
    if config.VOLUME_CONFIRMATION:
        volume = df["Volume"].squeeze()
        vol_ma = float(volume.rolling(config.VOLUME_MA_PERIOD).mean().iloc[-1])
        vol_today = float(volume.iloc[-1])
        vol_ratio = vol_today / vol_ma if vol_ma > 0 else 1.0
        if vol_ratio < config.VOLUME_MIN_RATIO:
            volume_ok = False
            reasons.append(f"Volume {vol_today:,.0f} below {config.VOLUME_MA_PERIOD}-day avg {vol_ma:,.0f} (ratio {vol_ratio:.2f}) — signal suppressed")

    # ── Trend Filter (SMA200) ──────────────────────────────────────────────────
    # Only allow BUY in uptrend, SELL in downtrend.
    final_signal = _apply_trend_filter(score, price, sma200, reasons, volume_ok=volume_ok and not near_earnings)

    return SignalResult(
        ticker=ticker,
        signal=final_signal,
        score=score,
        price=price,
        rsi=rsi,
        macd=macd_line,
        macd_signal_line=signal_line,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        reasons=reasons,
    )


def _apply_trend_filter(
    score: int,
    price: float,
    sma200: Optional[float],
    reasons: list[str],
    volume_ok: bool = True,
) -> Signal:
    """
    Apply the 200-day SMA trend filter and convert score to a Signal.

    Rule: Don't fight the macro trend.
    - If score says BUY but price < SMA200 → downgrade to HOLD.
    - If score says SELL but price > SMA200 → downgrade to HOLD.
    - If volume confirmation failed → downgrade to HOLD.
    """
    if score >= config.SIGNAL_BUY_THRESHOLD:
        raw_signal = Signal.BUY
    elif score <= config.SIGNAL_SELL_THRESHOLD:
        raw_signal = Signal.SELL
    else:
        return Signal.HOLD

    # Volume confirmation — suppress signal on low volume
    if not volume_ok:
        return Signal.HOLD

    # Apply trend filter only when SMA200 is available
    if sma200 is not None:
        in_uptrend = price > sma200
        if raw_signal == Signal.BUY and not in_uptrend:
            reasons.append(
                f"Trend filter: price {price:.2f} < SMA200 {sma200:.2f} → BUY downgraded to HOLD"
            )
            return Signal.HOLD
        if raw_signal == Signal.SELL and in_uptrend:
            reasons.append(
                f"Trend filter: price {price:.2f} > SMA200 {sma200:.2f} → SELL downgraded to HOLD"
            )
            return Signal.HOLD

    return raw_signal


def _has_earnings_soon(ticker: str) -> bool:
    """Return True if the ticker has earnings within EARNINGS_GUARD_DAYS."""
    if config.EARNINGS_GUARD_DAYS <= 0:
        return False
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None or cal.empty:
            return False
        # calendar has columns like 'Earnings Date'; handle both dict and DataFrame
        if hasattr(cal, 'columns'):
            col = next((c for c in cal.columns if 'Earnings' in c), None)
            if col is None:
                return False
            earnings_date = pd.to_datetime(cal[col].iloc[0]).date()
        else:
            earnings_date = pd.to_datetime(cal.get('Earnings Date', [None])[0]).date()
        today = date.today()
        days_away = (earnings_date - today).days
        if 0 <= days_away <= config.EARNINGS_GUARD_DAYS:
            logger.info("%s earnings in %d day(s) (%s) — suppressing BUY", ticker, days_away, earnings_date)
            return True
    except Exception:
        pass
    return False
