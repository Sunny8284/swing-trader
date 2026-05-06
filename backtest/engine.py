"""
backtest/engine.py — Walk-forward backtesting engine.

Uses the exact same signal logic and risk rules as the live bot:
  - RSI / MACD / SMA crossover / Bollinger Bands composite score
  - SMA200 trend filter (BUY only above, SELL only below)
  - 5% position sizing, 20% cash reserve
  - 3% stop-loss, 8% take-profit

The simulation is vectorised over the full date range using pre-computed
indicators, which are causal (no look-ahead bias).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta, datetime

import numpy as np
import pandas as pd
import ta
import yfinance as yf

import config

logger = logging.getLogger(__name__)

WARMUP_DAYS = 220   # enough bars to compute SMA200


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Trade:
    ticker: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    exit_reason: str   # stop_loss | take_profit | sell_signal | end_of_backtest


@dataclass
class BacktestResult:
    start_date: date
    end_date: date
    initial_capital: float
    final_capital: float
    total_return_pct: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_pnl: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)   # [{date, equity}]
    spy_return_pct: float = 0.0   # buy-and-hold SPY benchmark


# ── Signal computation (vectorised, causal) ────────────────────────────────────

def _compute_signals(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """
    Compute daily BUY/SELL/HOLD signals for the entire DataFrame at once.
    All indicators are causal — no look-ahead bias.
    `params` overrides config values for optimizer sweeps.

    Strategy enhancements (default off → preserves baseline behavior):
      regime_aware_rsi (bool): when SMA20>SMA50>SMA200 (full bullish stack),
        skip the RSI-overbought penalty. Symmetric for the bearish stack.
        Lets the strategy participate in trending stocks (e.g. AMD-style runs)
        instead of fighting the trend with mean-reversion.
    """
    p = params or {}
    buy_threshold     = p.get("buy_threshold",     config.SIGNAL_BUY_THRESHOLD)
    sell_threshold    = p.get("sell_threshold",    config.SIGNAL_SELL_THRESHOLD)
    rsi_oversold      = p.get("rsi_oversold",      config.RSI_OVERSOLD)
    rsi_overbought    = p.get("rsi_overbought",    config.RSI_OVERBOUGHT)
    regime_aware_rsi  = p.get("regime_aware_rsi",  False)

    close = df["Close"].squeeze()

    rsi = ta.momentum.RSIIndicator(close=close, window=config.RSI_PERIOD).rsi()

    macd_obj = ta.trend.MACD(
        close=close,
        window_fast=config.MACD_FAST,
        window_slow=config.MACD_SLOW,
        window_sign=config.MACD_SIGNAL,
    )
    macd_line   = macd_obj.macd()
    signal_line = macd_obj.macd_signal()
    macd_prev   = macd_line.shift(1)
    sig_prev    = signal_line.shift(1)

    sma20  = ta.trend.SMAIndicator(close=close, window=config.SMA_SHORT).sma_indicator()
    sma50  = ta.trend.SMAIndicator(close=close, window=config.SMA_LONG).sma_indicator()
    sma200 = ta.trend.SMAIndicator(close=close, window=config.SMA_TREND).sma_indicator()

    bb    = ta.volatility.BollingerBands(close=close, window=config.BOLLINGER_PERIOD, window_dev=config.BOLLINGER_STD)
    bb_hi = bb.bollinger_hband()
    bb_lo = bb.bollinger_lband()

    score = pd.Series(0, index=close.index, dtype=int)

    if regime_aware_rsi:
        full_bull_stack = (sma20 > sma50) & (sma50 > sma200)
        full_bear_stack = (sma20 < sma50) & (sma50 < sma200)
        score += ((rsi < rsi_oversold) & ~full_bear_stack).astype(int)
        score -= ((rsi > rsi_overbought) & ~full_bull_stack).astype(int)
    else:
        score += (rsi < rsi_oversold).astype(int)
        score -= (rsi > rsi_overbought).astype(int)

    score += ((macd_prev <= sig_prev) & (macd_line > signal_line)).astype(int)
    score -= ((macd_prev >= sig_prev) & (macd_line < signal_line)).astype(int)
    score += (sma20 > sma50).astype(int)
    score -= (sma20 <= sma50).astype(int)
    score += (close < bb_lo).astype(int)
    score -= (close > bb_hi).astype(int)

    sig = pd.Series("HOLD", index=close.index)
    sig[score >= buy_threshold]  = "BUY"
    sig[score <= sell_threshold] = "SELL"

    sig[(sig == "BUY")  & (close <= sma200)] = "HOLD"
    sig[(sig == "SELL") & (close >= sma200)] = "HOLD"

    return pd.DataFrame({
        "price":  close,
        "signal": sig,
        "score":  score,
        "sma200": sma200,
    })


# ── Simulation ─────────────────────────────────────────────────────────────────

def run(
    tickers: list[str] | None = None,
    days: int = 365,
    initial_capital: float = 100_000.0,
    params: dict | None = None,
) -> BacktestResult:
    """
    Run a walk-forward backtest over the last `days` calendar days.

    Downloads (days + WARMUP_DAYS) of daily data, warms up indicators,
    then simulates the strategy from `start_date` to today.
    """
    tickers = tickers or config.WATCHLIST
    end_dt   = datetime.now()
    fetch_start = end_dt - timedelta(days=days + WARMUP_DAYS + 60)  # extra buffer

    logger.info("Downloading data for %d tickers (%d-day backtest)…", len(tickers), days)
    raw = yf.download(
        tickers,
        start=fetch_start.strftime("%Y-%m-%d"),
        end=end_dt.strftime("%Y-%m-%d"),
        auto_adjust=True,
        progress=False,
        group_by="ticker",
    )

    # Compute signals per ticker
    signals: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = raw.copy()
            else:
                df = raw.xs(ticker, axis=1, level="Ticker").dropna(how="all")
            if df.empty or len(df) < 60:
                logger.warning("Skipping %s — insufficient data", ticker)
                continue
            signals[ticker] = _compute_signals(df, params)
        except Exception as e:
            logger.warning("Signal computation failed for %s: %s", ticker, e)

    if not signals:
        raise RuntimeError("No signal data available for backtesting.")

    # Define backtest window (exclude warm-up)
    backtest_start = end_dt - timedelta(days=days)
    all_dates = sorted({
        d for df in signals.values()
        for d in df.index
        if pd.Timestamp(d) >= pd.Timestamp(backtest_start)
    })

    if not all_dates:
        raise RuntimeError("No trading days in the backtest window.")

    # SPY benchmark (buy-and-hold)
    spy_return_pct = _spy_benchmark(raw, all_dates, len(tickers))

    # Resolve risk params (allow override for optimizer)
    p             = params or {}
    stop_loss     = p.get("stop_loss_pct",   config.STOP_LOSS_PCT)
    take_profit   = p.get("take_profit_pct", config.TAKE_PROFIT_PCT)
    cooldown_days = int(p.get("cooldown_days", 0))  # min days held before signal-SELL fires
    reentry_cooldown_days = int(p.get("reentry_cooldown_days", 0))  # min days after exit before re-buying

    # Portfolio simulation
    capital   = initial_capital
    positions: dict[str, dict] = {}  # ticker → {qty, entry_price, entry_date}
    last_exit: dict[str, "date"] = {}  # ticker → exit_date (for re-entry cooldown)
    trades: list[Trade] = []
    equity_curve = []

    for dt in all_dates:
        ts = pd.Timestamp(dt)

        # Check exits first
        for ticker in list(positions.keys()):
            if ticker not in signals or ts not in signals[ticker].index:
                continue
            pos   = positions[ticker]
            price = float(signals[ticker].loc[ts, "price"])
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
            sig   = signals[ticker].loc[ts, "signal"]

            exit_reason = None
            if pnl_pct <= -stop_loss:
                exit_reason = "stop_loss"
                price = pos["entry_price"] * (1 - stop_loss)
            elif pnl_pct >= take_profit:
                exit_reason = "take_profit"
                price = pos["entry_price"] * (1 + take_profit)
            elif sig == "SELL":
                today = dt.date() if hasattr(dt, "date") else dt
                days_held = (today - pos["entry_date"]).days
                if days_held >= cooldown_days:
                    exit_reason = "sell_signal"

            if exit_reason:
                proceeds = price * pos["qty"]
                capital += proceeds
                pnl = proceeds - pos["entry_price"] * pos["qty"]
                exit_date = dt.date() if hasattr(dt, "date") else dt
                trades.append(Trade(
                    ticker=ticker,
                    entry_date=pos["entry_date"],
                    exit_date=exit_date,
                    entry_price=pos["entry_price"],
                    exit_price=price,
                    qty=pos["qty"],
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct * 100, 2),
                    exit_reason=exit_reason,
                ))
                last_exit[ticker] = exit_date
                del positions[ticker]

        # Check entries
        for ticker, sig_df in signals.items():
            if ticker in positions or ts not in sig_df.index:
                continue
            if sig_df.loc[ts, "signal"] != "BUY":
                continue

            # Re-entry cooldown: block re-buying a recently-exited ticker
            if reentry_cooldown_days > 0 and ticker in last_exit:
                today = dt.date() if hasattr(dt, "date") else dt
                if (today - last_exit[ticker]).days < reentry_cooldown_days:
                    continue

            price          = float(sig_df.loc[ts, "price"])
            position_value = initial_capital * config.MAX_POSITION_PCT
            min_cash       = initial_capital * config.MIN_CASH_RESERVE_PCT

            if capital - position_value < min_cash:
                continue

            qty      = position_value / price
            capital -= position_value
            positions[ticker] = {
                "qty":         qty,
                "entry_price": price,
                "entry_date":  dt.date() if hasattr(dt, "date") else dt,
            }

        # Mark-to-market equity
        pos_value = sum(
            positions[t]["qty"] * float(signals[t].loc[ts, "price"])
            for t in positions
            if t in signals and ts in signals[t].index
        )
        equity_curve.append({
            "date":   ts.strftime("%Y-%m-%d"),
            "equity": round(capital + pos_value, 2),
        })

    # Close remaining open positions at last available price
    for ticker, pos in positions.items():
        sig_df    = signals[ticker]
        last_ts   = sig_df.index[-1]
        price     = float(sig_df.loc[last_ts, "price"])
        proceeds  = price * pos["qty"]
        capital  += proceeds
        pnl       = proceeds - pos["entry_price"] * pos["qty"]
        pnl_pct   = (price - pos["entry_price"]) / pos["entry_price"]
        trades.append(Trade(
            ticker=ticker,
            entry_date=pos["entry_date"],
            exit_date=last_ts.date() if hasattr(last_ts, "date") else last_ts,
            entry_price=pos["entry_price"],
            exit_price=price,
            qty=pos["qty"],
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct * 100, 2),
            exit_reason="end_of_backtest",
        ))

    # Metrics
    wins       = [t for t in trades if t.pnl > 0]
    losses     = [t for t in trades if t.pnl <= 0]
    total_pnl  = sum(t.pnl for t in trades)
    win_rate   = round(len(wins) / len(trades) * 100, 1) if trades else 0.0
    avg_pnl    = round(total_pnl / len(trades), 2) if trades else 0.0
    total_ret  = round((capital - initial_capital) / initial_capital * 100, 2)
    max_dd     = _max_drawdown(equity_curve)
    sharpe     = _sharpe(equity_curve)

    return BacktestResult(
        start_date=all_dates[0].date() if hasattr(all_dates[0], "date") else all_dates[0],
        end_date=all_dates[-1].date() if hasattr(all_dates[-1], "date") else all_dates[-1],
        initial_capital=initial_capital,
        final_capital=round(capital, 2),
        total_return_pct=total_ret,
        total_trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        avg_pnl=avg_pnl,
        max_drawdown_pct=max_dd,
        sharpe_ratio=sharpe,
        trades=trades,
        equity_curve=equity_curve,
        spy_return_pct=spy_return_pct,
    )


def fetch_raw_data(tickers: list[str], days: int = 365) -> object:
    """Download OHLCV data once; reuse across many optimizer runs."""
    end_dt      = datetime.now()
    fetch_start = end_dt - timedelta(days=days + WARMUP_DAYS + 60)
    return yf.download(
        tickers,
        start=fetch_start.strftime("%Y-%m-%d"),
        end=end_dt.strftime("%Y-%m-%d"),
        auto_adjust=True,
        progress=False,
    )


def run_with_data(
    raw,
    tickers: list[str],
    days: int = 365,
    initial_capital: float = 100_000.0,
    params: dict | None = None,
) -> BacktestResult:
    """Same as run() but accepts pre-downloaded raw data — for the optimizer."""
    signals: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            df = raw.xs(ticker, axis=1, level="Ticker").dropna(how="all") if len(tickers) > 1 else raw.copy()
            if df.empty or len(df) < 60:
                continue
            signals[ticker] = _compute_signals(df, params)
        except Exception as e:
            logger.debug("Signal failed %s: %s", ticker, e)

    if not signals:
        raise RuntimeError("No signal data.")

    end_dt          = datetime.now()
    backtest_start  = end_dt - timedelta(days=days)
    all_dates       = sorted({
        d for df in signals.values()
        for d in df.index
        if pd.Timestamp(d) >= pd.Timestamp(backtest_start)
    })

    if not all_dates:
        raise RuntimeError("No trading days in window.")

    p           = params or {}
    stop_loss   = p.get("stop_loss_pct",   config.STOP_LOSS_PCT)
    take_profit = p.get("take_profit_pct", config.TAKE_PROFIT_PCT)
    cooldown_days = int(p.get("cooldown_days", 0))
    reentry_cooldown_days = int(p.get("reentry_cooldown_days", 0))

    capital   = initial_capital
    positions: dict[str, dict] = {}
    last_exit: dict[str, "date"] = {}
    trades: list[Trade] = []
    equity_curve = []

    for dt in all_dates:
        ts = pd.Timestamp(dt)

        for ticker in list(positions.keys()):
            if ticker not in signals or ts not in signals[ticker].index:
                continue
            pos     = positions[ticker]
            price   = float(signals[ticker].loc[ts, "price"])
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
            sig     = signals[ticker].loc[ts, "signal"]

            exit_reason = None
            if pnl_pct <= -stop_loss:
                exit_reason = "stop_loss"
                price = pos["entry_price"] * (1 - stop_loss)
            elif pnl_pct >= take_profit:
                exit_reason = "take_profit"
                price = pos["entry_price"] * (1 + take_profit)
            elif sig == "SELL":
                today = dt.date() if hasattr(dt, "date") else dt
                days_held = (today - pos["entry_date"]).days
                if days_held >= cooldown_days:
                    exit_reason = "sell_signal"

            if exit_reason:
                proceeds = price * pos["qty"]
                capital += proceeds
                pnl      = proceeds - pos["entry_price"] * pos["qty"]
                exit_date = dt.date() if hasattr(dt, "date") else dt
                trades.append(Trade(
                    ticker=ticker,
                    entry_date=pos["entry_date"],
                    exit_date=exit_date,
                    entry_price=pos["entry_price"],
                    exit_price=price,
                    qty=pos["qty"],
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct * 100, 2),
                    exit_reason=exit_reason,
                ))
                last_exit[ticker] = exit_date
                del positions[ticker]

        for ticker, sig_df in signals.items():
            if ticker in positions or ts not in sig_df.index:
                continue
            if sig_df.loc[ts, "signal"] != "BUY":
                continue
            if reentry_cooldown_days > 0 and ticker in last_exit:
                today = dt.date() if hasattr(dt, "date") else dt
                if (today - last_exit[ticker]).days < reentry_cooldown_days:
                    continue
            price          = float(sig_df.loc[ts, "price"])
            position_value = initial_capital * config.MAX_POSITION_PCT
            if capital - position_value < initial_capital * config.MIN_CASH_RESERVE_PCT:
                continue
            qty      = position_value / price
            capital -= position_value
            positions[ticker] = {
                "qty": qty, "entry_price": price,
                "entry_date": dt.date() if hasattr(dt, "date") else dt,
            }

        pos_value = sum(
            positions[t]["qty"] * float(signals[t].loc[ts, "price"])
            for t in positions if t in signals and ts in signals[t].index
        )
        equity_curve.append({"date": ts.strftime("%Y-%m-%d"), "equity": round(capital + pos_value, 2)})

    for ticker, pos in positions.items():
        sig_df  = signals[ticker]
        last_ts = sig_df.index[-1]
        price   = float(sig_df.loc[last_ts, "price"])
        proceeds = price * pos["qty"]
        capital += proceeds
        pnl      = proceeds - pos["entry_price"] * pos["qty"]
        pnl_pct  = (price - pos["entry_price"]) / pos["entry_price"]
        trades.append(Trade(
            ticker=ticker,
            entry_date=pos["entry_date"],
            exit_date=last_ts.date() if hasattr(last_ts, "date") else last_ts,
            entry_price=pos["entry_price"],
            exit_price=price,
            qty=pos["qty"],
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct * 100, 2),
            exit_reason="end_of_backtest",
        ))

    wins       = [t for t in trades if t.pnl > 0]
    losses     = [t for t in trades if t.pnl <= 0]
    total_pnl  = sum(t.pnl for t in trades)
    win_rate   = round(len(wins) / len(trades) * 100, 1) if trades else 0.0
    avg_pnl    = round(total_pnl / len(trades), 2) if trades else 0.0
    total_ret  = round((capital - initial_capital) / initial_capital * 100, 2)

    return BacktestResult(
        start_date=all_dates[0].date() if hasattr(all_dates[0], "date") else all_dates[0],
        end_date=all_dates[-1].date() if hasattr(all_dates[-1], "date") else all_dates[-1],
        initial_capital=initial_capital,
        final_capital=round(capital, 2),
        total_return_pct=total_ret,
        total_trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        avg_pnl=avg_pnl,
        max_drawdown_pct=_max_drawdown(equity_curve),
        sharpe_ratio=_sharpe(equity_curve),
        trades=trades,
        equity_curve=equity_curve,
        spy_return_pct=0.0,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _max_drawdown(equity_curve: list[dict]) -> float:
    if not equity_curve:
        return 0.0
    values = [p["equity"] for p in equity_curve]
    peak   = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def _sharpe(equity_curve: list[dict], risk_free: float = 0.0) -> float:
    if len(equity_curve) < 2:
        return 0.0
    values  = np.array([p["equity"] for p in equity_curve], dtype=float)
    returns = np.diff(values) / values[:-1]
    if returns.std() == 0:
        return 0.0
    daily_sharpe = (returns.mean() - risk_free / 252) / returns.std()
    return round(float(daily_sharpe * np.sqrt(252)), 2)


def _spy_benchmark(raw, all_dates: list, n_tickers: int) -> float:
    """Buy-and-hold SPY return over the same period."""
    try:
        tickers_in_raw = raw.columns.get_level_values("Ticker").unique().tolist()
        if "SPY" not in tickers_in_raw:
            return 0.0
        spy = raw.xs("SPY", axis=1, level="Ticker")["Close"].dropna()
        if spy.empty:
            return 0.0
        # Normalise to timezone-naive for comparison
        if spy.index.tz is not None:
            spy.index = spy.index.tz_convert(None)
        start_ts  = pd.Timestamp(all_dates[0]).normalize()
        end_ts    = pd.Timestamp(all_dates[-1]).normalize()
        spy_slice = spy[(spy.index >= start_ts) & (spy.index <= end_ts)]
        if len(spy_slice) < 2:
            return 0.0
        return round((float(spy_slice.iloc[-1]) - float(spy_slice.iloc[0])) / float(spy_slice.iloc[0]) * 100, 2)
    except Exception:
        return 0.0
