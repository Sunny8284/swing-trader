"""
agent/trader.py — The trading agent.

The agent sits between signal generation and order execution. Its job:

1. Receive a list of SignalResult objects.
2. Apply additional filters / guardrails (e.g. don't over-trade, check
   existing positions, respect daily loss limits).
3. Decide which signals to act on.
4. Dispatch to the executor.
5. Snapshot the portfolio after each cycle.

Design philosophy
─────────────────
The agent is intentionally simple — it trusts the signal generator for
entry/exit logic and focuses on risk management and execution discipline.
Think of it as the "portfolio manager" layer that sits above raw signals.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import config
from db import storage
from executor import trade_executor
from signals.generator import Signal, SignalResult

logger = logging.getLogger(__name__)


@dataclass
class AgentAction:
    ticker: str
    action: str         # "BUY" | "SELL" | "HOLD" | "SKIPPED"
    reason: str
    order_result: Optional[dict] = None


class TradingAgent:
    """
    Stateless trading agent — all state is persisted to the DB.

    Call `run_cycle(signals)` once per trading session.
    """

    def __init__(self) -> None:
        self.daily_loss_limit_pct: float = 0.03   # stop trading if day P&L < -3%
        self._start_equity: Optional[float] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def run_cycle(self, signals: list[SignalResult]) -> list[AgentAction]:
        """
        Process all signals and execute approved trades.

        Returns a list of AgentAction describing what was done for each ticker.
        """
        logger.info("Agent cycle starting — %d signals to evaluate.", len(signals))
        actions: list[AgentAction] = []

        # Check daily loss limit before doing anything
        if self._daily_loss_limit_breached():
            logger.warning("Daily loss limit breached — no new trades this cycle.")
            return [
                AgentAction(ticker=s.ticker, action="SKIPPED", reason="daily loss limit breached")
                for s in signals
            ]

        # Check stop-loss / take-profit on existing positions before evaluating new signals
        exit_actions = self._check_exit_conditions()
        actions.extend(exit_actions)

        for signal_result in signals:
            action = self._evaluate(signal_result)
            actions.append(action)
            logger.info(
                "Agent decision for %s: %s — %s",
                action.ticker, action.action, action.reason,
            )

        # Save a portfolio snapshot at the end of the cycle
        self._snapshot_portfolio()

        buy_count = sum(1 for a in actions if a.action == "BUY")
        sell_count = sum(1 for a in actions if a.action == "SELL")
        hold_count = sum(1 for a in actions if a.action == "HOLD")
        logger.info(
            "Cycle complete: %d BUY, %d SELL, %d HOLD, %d SKIPPED",
            buy_count, sell_count, hold_count,
            len(actions) - buy_count - sell_count - hold_count,
        )

        return actions

    # ── Internal logic ─────────────────────────────────────────────────────────

    def _evaluate(self, result: SignalResult) -> AgentAction:
        """Apply guardrails and execute a single signal."""

        ticker = result.ticker
        signal = result.signal
        price = result.price

        # ── HOLD: nothing to do ───────────────────────────────────────────────
        if signal == Signal.HOLD:
            return AgentAction(ticker=ticker, action="HOLD", reason="signal is HOLD")

        # ── BUY ───────────────────────────────────────────────────────────────
        if signal == Signal.BUY:
            # Guard: don't open a position we already own
            if trade_executor.has_position(ticker):
                return AgentAction(
                    ticker=ticker,
                    action="SKIPPED",
                    reason="already have an open position",
                )

            order = trade_executor.execute_buy(ticker=ticker, price=price)
            if order:
                return AgentAction(
                    ticker=ticker,
                    action="BUY",
                    reason=f"score={result.score:+d} — {'; '.join(result.reasons[:2])}",
                    order_result=order,
                )
            else:
                return AgentAction(
                    ticker=ticker,
                    action="SKIPPED",
                    reason="BUY signal but insufficient buying power",
                )

        # ── SELL ──────────────────────────────────────────────────────────────
        if signal == Signal.SELL:
            order = trade_executor.execute_sell(ticker=ticker, price=price)
            if order:
                return AgentAction(
                    ticker=ticker,
                    action="SELL",
                    reason=f"score={result.score:+d} — {'; '.join(result.reasons[:2])}",
                    order_result=order,
                )
            else:
                return AgentAction(
                    ticker=ticker,
                    action="SKIPPED",
                    reason="SELL signal but no open position to close",
                )

        # Fallback (shouldn't reach here)
        return AgentAction(ticker=ticker, action="HOLD", reason="unknown signal state")

    def _check_exit_conditions(self) -> list[AgentAction]:
        """
        Scan open positions and trigger market sells when stop-loss or
        take-profit thresholds are hit.

        Alpaca's unrealized_plpc is a decimal fraction (0.05 = 5%).
        config.STOP_LOSS_PCT and TAKE_PROFIT_PCT are also decimal fractions.
        """
        actions: list[AgentAction] = []
        try:
            positions = trade_executor.get_positions()
        except Exception as exc:
            logger.warning("Could not fetch positions for exit check: %s", exc)
            return actions

        for pos in positions:
            ticker = pos["ticker"]
            plpc = pos["unrealized_plpc"]   # raw decimal from Alpaca, e.g. 0.089
            current_price = pos["current_price"]
            unrealized_pl = pos["unrealized_pl"]

            # Update peak price and check trailing stop
            peak_price = storage.update_peak_price(ticker, current_price)

            reason: Optional[str] = None
            if plpc <= -config.STOP_LOSS_PCT:
                reason = (
                    f"Stop-loss triggered: {plpc*100:.1f}% "
                    f"(limit: -{config.STOP_LOSS_PCT*100:.0f}%)"
                )
            elif peak_price and current_price <= peak_price * (1 - config.TRAILING_STOP_PCT):
                reason = (
                    f"Trailing stop triggered: price ${current_price:.2f} fell "
                    f"{config.TRAILING_STOP_PCT*100:.0f}% below peak ${peak_price:.2f}"
                )

            if reason:
                logger.info("Exit condition for %s: %s", ticker, reason)
                order = trade_executor.execute_sell(ticker=ticker, price=current_price)
                if order:
                    storage.close_trade(ticker=ticker, exit_price=current_price, pnl=unrealized_pl)
                    actions.append(AgentAction(
                        ticker=ticker,
                        action="SELL",
                        reason=reason,
                        order_result=order,
                    ))

        return actions

    def _daily_loss_limit_breached(self) -> bool:
        """
        Return True if the portfolio has declined more than the daily loss limit.

        We compare current equity against the first snapshot of today.
        This is a safety mechanism to stop trading on a bad day.
        """
        try:
            acct = trade_executor.get_account()
            current_equity = acct["equity"]

            # Get today's first snapshot from DB to use as the day's starting equity
            snapshots = storage.get_portfolio_history(limit=1)
            if not snapshots:
                return False

            start_equity = snapshots[-1].equity  # oldest in our small window
            if start_equity <= 0:
                return False

            daily_return = (current_equity - start_equity) / start_equity
            if daily_return < -self.daily_loss_limit_pct:
                logger.warning(
                    "Daily loss limit hit: %.2f%% (limit: %.2f%%)",
                    daily_return * 100,
                    self.daily_loss_limit_pct * 100,
                )
                return True
        except Exception as exc:
            logger.warning("Could not check daily loss limit: %s", exc)

        return False

    def _snapshot_portfolio(self) -> None:
        """Save a portfolio snapshot to the DB."""
        try:
            acct = trade_executor.get_account()
            positions = trade_executor.get_positions()
            storage.save_portfolio_snapshot(
                equity=acct["equity"],
                cash=acct["cash"],
                buying_power=acct["buying_power"],
                position_count=len(positions),
            )
        except Exception as exc:
            logger.warning("Could not save portfolio snapshot: %s", exc)
