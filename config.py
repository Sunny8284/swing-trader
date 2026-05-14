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
SIGNAL_BUY_THRESHOLD: int = 1
SIGNAL_SELL_THRESHOLD: int = -1  # Minimum bearish score to issue SELL

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
MAX_POSITION_PCT: float = 0.05      # 5% per position
# Stop-loss below entry price.
STOP_LOSS_PCT: float = 0.02         # 2% stop loss
# Take-profit above entry price.
TAKE_PROFIT_PCT: float = 0.06       # 6% take profit
# Minimum cash reserve — never deploy more than this fraction of portfolio.
MIN_CASH_RESERVE_PCT: float = 0.20  # Keep 20% in cash at all times

# ── Scheduler ─────────────────────────────────────────────────────────────────
# Cron expression for when to run the main trading loop.
# Default: weekdays at 09:35 ET (5 minutes after market open).
SCHEDULE_CRON: dict = {
    "hour": 9,
    "minute": 35,
    "day_of_week": "mon-fri",
}

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///swing_trader.db")

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR: str = "logs"
