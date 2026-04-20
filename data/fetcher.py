"""
data/fetcher.py — Market data retrieval using yfinance.

Fetches OHLCV (Open, High, Low, Close, Volume) daily bars for a list of
tickers. Results are returned as a dict of DataFrames, one per ticker.

yfinance is free and requires no API key for historical data.
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf

import config

logger = logging.getLogger(__name__)


def fetch_ohlcv(
    tickers: list[str],
    period: str = config.DATA_PERIOD,
    interval: str = config.DATA_INTERVAL,
) -> dict[str, pd.DataFrame]:
    """
    Download historical OHLCV bars for a list of tickers.

    Args:
        tickers:  List of US stock ticker symbols (e.g. ["AAPL", "MSFT"]).
        period:   yfinance period string — "3mo", "6mo", "1y", etc.
        interval: Bar size — "1d" for daily (standard for swing trading).

    Returns:
        Dict mapping ticker → DataFrame with columns
        [Open, High, Low, Close, Volume] indexed by date.
        Tickers that fail to download are omitted from the result.
    """
    if not tickers:
        return {}

    logger.info("Fetching data for %d tickers: %s", len(tickers), tickers)

    result: dict[str, pd.DataFrame] = {}

    # Download all tickers in a single batch request for efficiency.
    try:
        raw = yf.download(
            tickers=tickers,
            period=period,
            interval=interval,
            auto_adjust=True,   # adjust for splits/dividends automatically
            progress=False,
            group_by="ticker",
        )
    except Exception as exc:
        logger.error("yfinance download failed: %s", exc)
        return {}

    # yfinance returns a flat DataFrame when there's only one ticker,
    # and a multi-level DataFrame when there are multiple.
    if len(tickers) == 1:
        ticker = tickers[0]
        df = _clean(raw, ticker)
        if df is not None:
            result[ticker] = df
    else:
        for ticker in tickers:
            try:
                df = _clean(raw[ticker], ticker)
                if df is not None:
                    result[ticker] = df
            except KeyError:
                logger.warning("No data returned for %s — skipping.", ticker)

    logger.info("Successfully fetched data for %d/%d tickers.", len(result), len(tickers))
    return result


def fetch_single(
    ticker: str,
    period: str = config.DATA_PERIOD,
    interval: str = config.DATA_INTERVAL,
) -> Optional[pd.DataFrame]:
    """Convenience wrapper for a single ticker."""
    data = fetch_ohlcv([ticker], period=period, interval=interval)
    return data.get(ticker)


def _clean(df: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    """
    Validate and clean a raw yfinance DataFrame.

    - Drops rows with NaN in critical columns.
    - Ensures the index is a DatetimeIndex.
    - Returns None if the DataFrame is empty after cleaning.
    """
    if df is None or df.empty:
        logger.warning("Empty DataFrame for %s.", ticker)
        return None

    # Flatten MultiIndex columns returned by newer yfinance versions
    # e.g. ("Close", "AAPL") → "Close"
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        logger.warning("Missing columns %s for %s.", missing, ticker)
        return None

    df = df[required_cols].dropna()

    if df.empty:
        logger.warning("All rows NaN for %s after cleaning.", ticker)
        return None

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    df.index.name = "Date"
    logger.debug("Fetched %d bars for %s (latest: %s).", len(df), ticker, df.index[-1].date())
    return df


def latest_price(ticker: str) -> Optional[float]:
    """Return the most recent closing price for a ticker."""
    df = fetch_single(ticker, period="5d")
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])
