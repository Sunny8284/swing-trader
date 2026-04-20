"""
Swing Trader Dry Run — Signal Analysis
Fetches live market data and shows BUY/SELL/HOLD signals for all 18 watchlist stocks.
No trades are placed.

Run: python dry_run.py
"""

import sys
import subprocess

# Auto-install dependencies if missing
for pkg in ["yfinance", "pandas", "numpy", "ta"]:
    try:
        __import__(pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

import yfinance as yf
import pandas as pd
import numpy as np
import ta
from datetime import datetime

WATCHLIST = ["AAPL", "MSFT", "GOOGL", "NVDA", "AMD", "TSLA", "META", "AMZN",
             "JPM", "BAC", "V", "MA", "JNJ", "UNH", "XOM", "CVX", "SPY", "QQQ"]

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

def get_rsi_signal(rsi):
    if rsi < RSI_OVERSOLD:
        return "BUY"
    elif rsi > RSI_OVERBOUGHT:
        return "SELL"
    return "HOLD"

def get_macd_signal(df):
    macd = ta.trend.MACD(df["Close"].squeeze())
    macd_line = macd.macd().iloc[-1]
    signal_line = macd.macd_signal().iloc[-1]
    prev_macd = macd.macd().iloc[-2]
    prev_signal = macd.macd_signal().iloc[-2]
    # Bullish crossover
    if prev_macd <= prev_signal and macd_line > signal_line:
        return "BUY"
    # Bearish crossover
    elif prev_macd >= prev_signal and macd_line < signal_line:
        return "SELL"
    return "HOLD"

def get_ma_signal(df):
    close = df["Close"].squeeze()
    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma200 = float(close.rolling(200).mean().iloc[-1])
    price = float(close.iloc[-1])
    if price > ma50 and ma50 > ma200:
        return "BUY"   # Golden cross territory
    elif price < ma50 and ma50 < ma200:
        return "SELL"  # Death cross territory
    return "HOLD"

def majority_vote(signals):
    buys = signals.count("BUY")
    sells = signals.count("SELL")
    if buys > sells and buys >= 2:
        return "BUY"
    elif sells > buys and sells >= 2:
        return "SELL"
    return "HOLD"

print(f"\n{'='*75}")
print(f"  SWING TRADER DRY RUN — {datetime.now().strftime('%A %B %d, %Y %I:%M %p')}")
print(f"{'='*75}")
print(f"{'Ticker':<8} {'Price':>8} {'RSI':>6} {'RSI Sig':<10} {'MACD Sig':<11} {'MA Sig':<9} {'FINAL':<8}")
print(f"{'-'*75}")

results = []
errors = []

for ticker in WATCHLIST:
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 200:
            errors.append(f"{ticker}: insufficient data")
            continue

        # Flatten MultiIndex columns (newer yfinance versions)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        price = float(df["Close"].iloc[-1])
        rsi_series = ta.momentum.RSIIndicator(df["Close"].squeeze(), window=14).rsi()
        rsi = float(rsi_series.iloc[-1])

        rsi_sig = get_rsi_signal(rsi)
        macd_sig = get_macd_signal(df)
        ma_sig = get_ma_signal(df)
        final = majority_vote([rsi_sig, macd_sig, ma_sig])

        emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(final, "")
        print(f"{ticker:<8} {price:>8.2f} {rsi:>6.1f} {rsi_sig:<10} {macd_sig:<11} {ma_sig:<9} {emoji} {final}")
        results.append({"ticker": ticker, "price": price, "rsi": rsi,
                         "rsi_sig": rsi_sig, "macd_sig": macd_sig, "ma_sig": ma_sig, "final": final})
    except Exception as e:
        errors.append(f"{ticker}: {e}")

print(f"{'='*75}")
buys  = [r["ticker"] for r in results if r["final"] == "BUY"]
sells = [r["ticker"] for r in results if r["final"] == "SELL"]
holds = [r["ticker"] for r in results if r["final"] == "HOLD"]
print(f"\n  🟢 BUY  ({len(buys)}):  {', '.join(buys) or 'none'}")
print(f"  🔴 SELL ({len(sells)}):  {', '.join(sells) or 'none'}")
print(f"  🟡 HOLD ({len(holds)}):  {', '.join(holds) or 'none'}")

if errors:
    print(f"\n  ⚠️  Errors: {'; '.join(errors)}")

print()
