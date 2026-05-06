"""
backtest/variants.py — Compare strategy enhancement variants.

Runs the backtest under several configurations and prints a side-by-side
comparison of total return, Sharpe, win rate, max drawdown, and trade count.

Variants tested:
  V0  baseline                          — current production strategy
  V1  +regime_aware_rsi                 — skip RSI overbought/oversold penalty
                                          when SMA stack is fully aligned
  V2  +cooldown_days=3                  — block signal-driven SELL within 3 days
                                          of entry (TP/SL still fire)
  V3  +diversified watchlist            — adds energy/healthcare/consumer tickers
  V4  all three combined                — V1 + V2 + V3

Usage:
    venv/bin/python -m backtest.variants
    venv/bin/python -m backtest.variants --days 365
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

import config
from backtest.engine import run

logger = logging.getLogger(__name__)


# Watchlist additions: sectors currently underrepresented in config.WATCHLIST
DIVERSIFIED_ADDITIONS = [
    "COP",   # ConocoPhillips    — Energy
    "LLY",   # Eli Lilly         — Healthcare/Pharma
    "PFE",   # Pfizer            — Healthcare/Pharma
    "COST",  # Costco            — Consumer Staples
    "WMT",   # Walmart           — Consumer Staples
    "HD",    # Home Depot        — Consumer Discretionary
    "CAT",   # Caterpillar       — Industrials
]


@dataclass
class Variant:
    name: str
    desc: str
    tickers: list[str]
    params: dict


def variants() -> list[Variant]:
    base = config.WATCHLIST
    expanded = base + DIVERSIFIED_ADDITIONS

    return [
        Variant("V0 baseline",        "current production",            base,     {}),
        Variant("V1 regime_rsi",      "skip RSI penalty in trend",     base,     {"regime_aware_rsi": True}),
        Variant("V2 cooldown=3",      "no signal-SELL <3 days held",   base,     {"cooldown_days": 3}),
        Variant("V3 diversified",     f"+{len(DIVERSIFIED_ADDITIONS)} tickers (energy/HC/cons)", expanded, {}),
        Variant("V4 V1+V2+V3",        "regime + cooldown + diversified", expanded, {"regime_aware_rsi": True, "cooldown_days": 3}),
        Variant("V5 reentry_cd=5",    "no re-buy <5 days after exit",  base,     {"reentry_cooldown_days": 5}),
        Variant("V6 full stack",      "V4 + reentry_cooldown=5",       expanded, {"regime_aware_rsi": True, "cooldown_days": 3, "reentry_cooldown_days": 5}),
    ]


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def main(days: int = 365) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    print(f"\nRunning {len(variants())} variants over {days} calendar days "
          f"(~{int(days/365*252)} trading days)…\n")

    results = []
    for v in variants():
        print(f"  {v.name:<22} {v.desc} ({len(v.tickers)} tickers)…", end="", flush=True)
        try:
            r = run(tickers=v.tickers, days=days, params=v.params)
            results.append((v, r))
            print(f"  ret={r.total_return_pct:+.2f}%  trades={r.total_trades}  sharpe={r.sharpe_ratio}")
        except Exception as e:
            print(f"  FAILED: {e}")
            results.append((v, None))

    print()
    if not any(r for _, r in results):
        print("All variants failed.")
        return 1

    # Use V0's SPY benchmark (same period for all)
    spy_ret = next((r.spy_return_pct for _, r in results if r), 0.0)

    # Print comparison table
    print("═" * 102)
    print(f"{'Variant':<22}  {'Return':>10}  {'vs SPY':>10}  {'Trades':>7}  {'Win%':>6}  "
          f"{'AvgPnL':>9}  {'MaxDD':>7}  {'Sharpe':>7}")
    print("─" * 102)
    for v, r in results:
        if r is None:
            print(f"{v.name:<22}  {'—':>10}  (failed)")
            continue
        vs_spy = r.total_return_pct - spy_ret
        print(f"{v.name:<22}  {fmt_pct(r.total_return_pct):>10}  {fmt_pct(vs_spy):>10}  "
              f"{r.total_trades:>7}  {r.win_rate:>5.1f}%  ${r.avg_pnl:>7.0f}  "
              f"{r.max_drawdown_pct:>6.2f}%  {r.sharpe_ratio:>7}")
    print("═" * 102)
    print(f"  SPY buy-and-hold benchmark: {fmt_pct(spy_ret)}")
    print()

    # Highlight winner by Sharpe (risk-adjusted return)
    valid = [(v, r) for v, r in results if r]
    by_sharpe = sorted(valid, key=lambda x: x[1].sharpe_ratio, reverse=True)
    by_return = sorted(valid, key=lambda x: x[1].total_return_pct, reverse=True)

    print(f"  Best by Sharpe:  {by_sharpe[0][0].name}  ({by_sharpe[0][1].sharpe_ratio})")
    print(f"  Best by return:  {by_return[0][0].name}  ({fmt_pct(by_return[0][1].total_return_pct)})")
    print()

    # Per-variant exit-reason breakdown
    print("Exit-reason breakdown:")
    print(f"  {'Variant':<22}  {'TP':>5}  {'SL':>5}  {'sell_sig':>9}  {'EOB':>5}")
    for v, r in results:
        if r is None:
            continue
        from collections import Counter
        c = Counter(t.exit_reason for t in r.trades)
        print(f"  {v.name:<22}  {c.get('take_profit',0):>5}  {c.get('stop_loss',0):>5}  "
              f"{c.get('sell_signal',0):>9}  {c.get('end_of_backtest',0):>5}")
    print()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365, help="lookback window")
    args = parser.parse_args()
    sys.exit(main(args.days))
