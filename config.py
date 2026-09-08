"""
config.py — Central configuration for the Swing Trader system.

All runtime settings live here. Environment-specific secrets are loaded
from the .env file via python-dotenv so they never appear in source code.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Alpaca Paper Trading ───────────────────────────────────────────────────────
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL: str = os.getenv(
    "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
)

# ── Watchlist ──────────────────────────────────────────────────────────────────
# These are the tickers the system will monitor and potentially trade.
# Focused on liquid large/mid-cap US stocks — good for swing trading.
WATCHLIST: list[str] = [
    # Tech
    "AAPL",  # Apple
    "MSFT",  # Microsoft
    "GOOGL", # Alphabet
    "NVDA",  # NVIDIA
    "AMD",   # Advanced Micro Devices
    "TSLA",  # Tesla
    "META",  # Meta
    "AMZN",  # Amazon
    # Finance
    "JPM",   # JPMorgan Chase
    "BAC",   # Bank of America
    "V",     # Visa
    "MA",    # Mastercard
    # Healthcare
    "JNJ",   # Johnson & Johnson
    "UNH",   # UnitedHealth
    # Energy
    "XOM",   # ExxonMobil
    "CVX",   # Chevron
    # ETFs (broad market exposure / hedging)
    "SPY",   # S&P 500 ETF
    "QQQ",   # Nasdaq-100 ETF
    # Small-cap AI / quantum (high volatility ~5-6% daily — added 2026-05-11)
    "BBAI",  # BigBear.ai
    "RGTI",  # Rigetti Computing
    # V4 diversification additions (validated 2026-05-16)
    "COP",   # ConocoPhillips
    "LLY",   # Eli Lilly
    "PFE",   # Pfizer
    "COST",  # Costco
    "WMT",   # Walmart
    "HD",    # Home Depot
    "CAT",   # Caterpillar
]

# ── Data Fetching ──────────────────────────────────────────────────────────────
# How much historical data to pull for indicator calculation.
# Swing trading typically looks at daily bars over several months.
DATA_PERIOD: str = "6mo"       # yfinance period string (e.g. "3mo", "6mo", "1y")
DATA_INTERVAL: str = "1d"      # bar interval — daily for swing trading

# ── Signal Parameters ──────────────────────────────────────────────────────────
RSI_PERIOD: int = 14
RSI_OVERSOLD: float = 30.0     # RSI below this → bullish signal
RSI_OVERBOUGHT: float = 70.0   # RSI above this → bearish signal

MACD_FAST: int = 12
MACD_SLOW: int = 26
MACD_SIGNAL: int = 9

SMA_SHORT: int = 20            # Short-term trend
SMA_LONG: int = 50             # Medium-term trend
SMA_TREND: int = 200           # Long-term trend filter

BOLLINGER_PERIOD: int = 20
BOLLINGER_STD: float = 2.0

# Minimum number of bullish sub-signals required to issue a BUY.
# Max is 4 (RSI + MACD + MA crossover + price vs BB).
SIGNAL_BUY_THRESHOLD: int = 2        # raised from 1 → fewer but higher-conviction buys
SIGNAL_SELL_THRESHOLD: int = -1  # Minimum bearish score to issue SELL

# Regime-aware RSI (V4): suppress RSI signals that contradict the SMA trend stack.
# When SMA20>SMA50>SMA200 (full bull), ignore RSI overbought penalty.
# When SMA20<SMA50<SMA200 (full bear), ignore RSI oversold bonus.
REGIME_AWARE_RSI: bool = True

# ── Signal Filters ─────────────────────────────────────────────────────────────
# Volume confirmation: only act on signals where volume > N-day average.
VOLUME_CONFIRMATION: bool = True
VOLUME_MA_PERIOD: int = 20         # rolling average window
VOLUME_MIN_RATIO: float = 1.0      # require at least 1.0× avg volume (i.e. above avg)

# Earnings guard: skip BUY signals within this many days of earnings date.
EARNINGS_GUARD_DAYS: int = 2

# VIX-based position sizing: reduce position size when market fear is elevated.
VIX_SIZING: bool = True
VIX_HIGH_THRESHOLD: float = 25.0   # VIX above this → use reduced position size
VIX_HIGH_POSITION_PCT: float = 0.025  # 2.5% per position when VIX is high (vs 5% normal)

# ── Risk Management ────────────────────────────────────────────────────────────
# Maximum fraction of portfolio to allocate to a single position.
MAX_POSITION_PCT: float = 0.08      # 8% per position (raised from 5%)
# Stop-loss below entry price (hard floor on Alpaca bracket order).
STOP_LOSS_PCT: float = 0.015        # fallback only — see ATR_STOP_MULT below

# Volatility-scaled stops. A flat 1.5% sits INSIDE one day's normal range for
# every name on the watchlist (measured: NVDA 3.0%, META 2.9%, AAPL 2.2%,
# COST 1.8% average daily range), so it was firing on noise rather than on the
# trade being wrong — 34 of 46 backtested exits were stops, and the live win
# rate was 33%. Scaling the stop to each stock's own ATR lifts the backtested
# win rate to ~33-37% across 180/365/730-day windows.
ATR_STOP_MULT: float = 2.0          # stop = entry - 2.0 x ATR(14)
ATR_STOP_PERIOD: int = 14
ATR_STOP_MIN_PCT: float = 0.02      # never tighter than 2%
ATR_STOP_MAX_PCT: float = 0.12      # never wider than 12%
# Take-profit on Alpaca bracket — set high so bot-managed trailing stop fires first.
TAKE_PROFIT_PCT: float = 0.50       # 50% ceiling (effectively disabled)
# Trailing stop: sell if price falls this % below the position's peak price.
TRAILING_STOP_PCT: float = 0.06     # 6% trail below peak
# Minimum cash reserve — never deploy more than this fraction of portfolio.
MIN_CASH_RESERVE_PCT: float = 0.10  # 10% cash reserve (reduced from 20%)

# Round-trip execution cost assumed by the backtest, in basis points per side.
# Live orders are market orders filling at the open; the backtest used to fill
# at the signal bar's close with zero cost, which flattered every result.
SLIPPAGE_BPS: float = float(os.getenv("SLIPPAGE_BPS", "5"))   # 0.05% per side

# ── Scheduler ─────────────────────────────────────────────────────────────────
# Cron expression for when to run the main trading loop.
# Default: weekdays at 09:35 ET (5 minutes after market open).
SCHEDULE_CRON: dict = {
    "hour": 9,
    "minute": 35,
    "day_of_week": "mon-fri",
}

# ── Database ───────────────────────────────────────────────────────────────────
# ── AI reasoning (Groq) ───────────────────────────────────────────────────────
# Groq retires hosted models without notice: llama-3.1-8b-instant vanished and
# every reasoning call 404'd for months, silently, because failures are caught
# and logged as warnings. Keep the model name here so replacing it is a one-line
# change, and run `client.models.list()` to see what the key can currently reach.
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")

# ── Cash sleeve (core-satellite) ──────────────────────────────────────────────
# The strategy holds ~4 names and leaves ~50% of the account idle, which cannot
# keep up with a rising market however good the picks are. Park idle cash in an
# index ETF so uninvested capital still earns the market.
#
# PARK_TARGET_PCT is the dial between the two strategies. Backtested returns:
#            75%      90%     100%     SPY
#   180d  +11.79%  +15.49%  +16.80%  +16.91%
#   365d  +15.71%  +17.45%  +19.63%  +19.74%
#   730d  +53.61%  +47.73%  +44.17%  +44.29%
# Higher = closer to simply holding SPY. Lower = more exposure to the swing
# strategy, which beat SPY over 730 days and trailed it over 180 and 365.
PARK_IDLE_CASH_IN: str = os.getenv("PARK_IDLE_CASH_IN", "SPY")
PARK_TARGET_PCT: float = float(os.getenv("PARK_TARGET_PCT", "0.75"))
PARK_DRIFT_PCT: float = 0.05   # only rebalance past this drift, to avoid churn

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///swing_trader.db")

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR: str = "logs"
