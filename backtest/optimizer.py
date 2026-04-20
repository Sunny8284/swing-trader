"""
backtest/optimizer.py — Grid search over strategy parameters.

Downloads data once, then sweeps 36 parameter combinations and ranks
by Sharpe ratio. Takes ~30-40 seconds.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass

import config
from backtest.engine import fetch_raw_data, run_with_data

logger = logging.getLogger(__name__)

# ── Parameter grid ─────────────────────────────────────────────────────────────

PARAM_GRID = {
    "buy_threshold":  [1, 2, 3],
    "take_profit_pct": [0.06, 0.08, 0.10, 0.15],
    "stop_loss_pct":   [0.02, 0.03, 0.05],
}


@dataclass
class OptResult:
    buy_threshold:  int
    take_profit_pct: float
    stop_loss_pct:   float
    total_return_pct: float
    win_rate:        float
    total_trades:    int
    sharpe_ratio:    float
    max_drawdown_pct: float
    avg_pnl:         float
    is_current:      bool = False   # flags the current live config


def run_optimization(tickers: list[str] | None = None, days: int = 365) -> list[OptResult]:
    """
    Run all parameter combinations and return results sorted by Sharpe ratio.
    """
    tickers = tickers or config.WATCHLIST
    logger.info("Downloading data for optimizer (%d tickers, %d days)…", len(tickers), days)
    raw = fetch_raw_data(tickers, days=days)

    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(itertools.product(*values))
    logger.info("Running %d parameter combinations…", len(combos))

    results: list[OptResult] = []

    for i, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        # sell threshold mirrors buy threshold (symmetric)
        params["sell_threshold"] = -params["buy_threshold"]

        try:
            bt = run_with_data(raw, tickers, days=days, params=params)
            is_current = (
                params["buy_threshold"]   == config.SIGNAL_BUY_THRESHOLD and
                params["take_profit_pct"] == config.TAKE_PROFIT_PCT and
                params["stop_loss_pct"]   == config.STOP_LOSS_PCT
            )
            results.append(OptResult(
                buy_threshold=params["buy_threshold"],
                take_profit_pct=params["take_profit_pct"],
                stop_loss_pct=params["stop_loss_pct"],
                total_return_pct=bt.total_return_pct,
                win_rate=bt.win_rate,
                total_trades=bt.total_trades,
                sharpe_ratio=bt.sharpe_ratio,
                max_drawdown_pct=bt.max_drawdown_pct,
                avg_pnl=bt.avg_pnl,
                is_current=is_current,
            ))
            logger.info(
                "[%d/%d] buy=%d tp=%.0f%% sl=%.0f%% → return=%.1f%% sharpe=%.2f",
                i, len(combos),
                params["buy_threshold"],
                params["take_profit_pct"] * 100,
                params["stop_loss_pct"] * 100,
                bt.total_return_pct,
                bt.sharpe_ratio,
            )
        except Exception as e:
            logger.warning("Combo %s failed: %s", params, e)

    # Sort by Sharpe ratio (risk-adjusted), break ties by return
    results.sort(key=lambda r: (r.sharpe_ratio, r.total_return_pct), reverse=True)
    logger.info("Optimization complete. Best: %s", results[0] if results else "none")
    return results
