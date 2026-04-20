"""
main.py — Swing Trader entry point.

Modes
─────
  python main.py run       — Execute one trading cycle immediately.
  python main.py schedule  — Start the scheduler (runs daily at market open).
  python main.py api       — Start the FastAPI server on port 8000.
  python main.py status    — Print account summary and open positions.

The scheduler runs the trading cycle on weekday mornings (configured in
config.py → SCHEDULE_CRON), mimicking what you'd do manually on Robinhood
each morning.
"""

import argparse
import logging
import sys

import config
from db import storage
from data import fetcher
from signals import generator
from agent.trader import TradingAgent
from agent import reasoner as ai_reasoner
from agent import notifier

# ── Logging setup ──────────────────────────────────────────────────────────────

import os

os.makedirs(config.LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(config.LOG_DIR, "swing_trader.log")),
    ],
)
logger = logging.getLogger("main")


# ── Core trading cycle ─────────────────────────────────────────────────────────

def run_trading_cycle(watchlist: list[str] | None = None) -> None:
    """
    Full pipeline: fetch → signal → agent → execute → log.

    This is the function that runs on each scheduled tick.
    """
    watchlist = watchlist or config.WATCHLIST
    logger.info("=" * 60)
    logger.info("Trading cycle starting for %d tickers.", len(watchlist))
    logger.info("Watchlist: %s", watchlist)

    # ── Step 1: Fetch OHLCV data ───────────────────────────────────────────────
    logger.info("Step 1/4 — Fetching market data...")
    data = fetcher.fetch_ohlcv(watchlist)

    if not data:
        logger.error("No data fetched — aborting cycle.")
        return

    # ── Step 2: Generate signals ───────────────────────────────────────────────
    logger.info("Step 2/4 — Generating signals...")
    signal_results = generator.generate_signals(data)

    # Save every signal to DB, then attach Claude reasoning
    groq_key = os.environ.get("GROQ_API_KEY", "")
    for result in signal_results:
        record = storage.save_signal(result)
        if groq_key:
            reasoning = ai_reasoner.explain(result)
            if reasoning:
                storage.update_reasoning(record.id, reasoning)
                result._reasoning = reasoning

    # Print a clean signal summary to the console
    _print_signal_table(signal_results)

    # ── Step 3: Agent evaluates and acts ──────────────────────────────────────
    logger.info("Step 3/4 — Agent evaluating signals...")
    agent = TradingAgent()
    actions = agent.run_cycle(signal_results)

    # ── Step 4: Log results + notify ──────────────────────────────────────────
    logger.info("Step 4/4 — Logging results...")
    _print_action_summary(actions)

    try:
        from api.alpaca_client import trading_client
        acct = trading_client.get_account()
        account_data = {
            "portfolio_value": float(acct.portfolio_value or 0),
            "cash": float(acct.cash or 0),
        }
    except Exception:
        account_data = None
    notifier.send_cycle_summary(signal_results, actions, account=account_data)

    logger.info("Trading cycle complete.")
    logger.info("=" * 60)


# ── Scheduler ──────────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    """
    Start the APScheduler that fires run_trading_cycle on the configured schedule.

    Keeps the process alive. Use Ctrl-C to stop.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BlockingScheduler(timezone="America/New_York")
    trigger = CronTrigger(
        hour=config.SCHEDULE_CRON["hour"],
        minute=config.SCHEDULE_CRON["minute"],
        day_of_week=config.SCHEDULE_CRON["day_of_week"],
        timezone="America/New_York",
    )
    scheduler.add_job(run_trading_cycle, trigger=trigger, name="swing_trader")

    logger.info(
        "Scheduler started — will run at %02d:%02d ET on %s.",
        config.SCHEDULE_CRON["hour"],
        config.SCHEDULE_CRON["minute"],
        config.SCHEDULE_CRON["day_of_week"],
    )
    logger.info("Press Ctrl-C to stop.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


# ── API server ─────────────────────────────────────────────────────────────────

def start_api() -> None:
    """Launch the FastAPI server with uvicorn."""
    import uvicorn
    logger.info("Starting API server on http://localhost:8000")
    logger.info("Docs available at http://localhost:8000/docs")
    uvicorn.run("api.routes:app", host="0.0.0.0", port=8000, reload=False)


# ── Status ─────────────────────────────────────────────────────────────────────

def print_status() -> None:
    """Print current account summary and open positions."""
    try:
        from executor import trade_executor
        acct = trade_executor.get_account()
        positions = trade_executor.get_positions()

        print("\n── Account ─────────────────────────────────────────")
        print(f"  Equity:        ${acct['equity']:>12,.2f}")
        print(f"  Cash:          ${acct['cash']:>12,.2f}")
        print(f"  Buying Power:  ${acct['buying_power']:>12,.2f}")
        print(f"  Status:        {acct['status']}")

        print(f"\n── Open Positions ({len(positions)}) ──────────────────────────")
        if not positions:
            print("  (none)")
        else:
            print(f"  {'Ticker':<8} {'Qty':>6} {'Entry':>9} {'Current':>9} {'P&L':>10} {'%':>7}")
            print("  " + "-" * 55)
            for p in positions:
                pct = p["unrealized_plpc"] * 100
                print(
                    f"  {p['ticker']:<8} {p['qty']:>6.0f} "
                    f"${p['avg_entry_price']:>8.2f} ${p['current_price']:>8.2f} "
                    f"${p['unrealized_pl']:>9.2f} {pct:>6.1f}%"
                )
        print()
    except RuntimeError as exc:
        print(f"\nError: {exc}")
        print("Make sure ALPACA_API_KEY and ALPACA_SECRET_KEY are set in .env\n")


# ── Pretty-printing helpers ────────────────────────────────────────────────────

def _print_signal_table(signals) -> None:
    print("\n── Signal Summary ──────────────────────────────────────────────")
    print(f"  {'Ticker':<8} {'Signal':<6} {'Score':>6} {'Price':>9} {'RSI':>6}")
    print("  " + "-" * 42)
    for s in sorted(signals, key=lambda x: x.score, reverse=True):
        flag = "★" if s.signal.value != "HOLD" else " "
        print(
            f"{flag} {s.ticker:<8} {s.signal.value:<6} {s.score:>+6d} "
            f"${s.price:>8.2f} {s.rsi or 0:>5.1f}"
        )
    print()


def _print_action_summary(actions) -> None:
    executed = [a for a in actions if a.action in ("BUY", "SELL")]
    if not executed:
        logger.info("No orders submitted this cycle.")
        return
    for a in executed:
        logger.info("EXECUTED: %s %s — %s", a.action, a.ticker, a.reason)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Ensure the DB tables exist before anything else runs
    storage.init_db()

    parser = argparse.ArgumentParser(
        description="Swing Trader — automated US equity swing trading system"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="Run one trading cycle immediately.")
    sub.add_parser("schedule", help="Start the scheduler (runs at market open daily).")
    sub.add_parser("api", help="Start the FastAPI REST server on port 8000.")
    sub.add_parser("status", help="Print account summary and open positions.")

    args = parser.parse_args()

    # Default to "run" when invoked with no arguments (e.g. from launchd scheduler)
    if not args.command:
        args.command = "run"

    if args.command == "run":
        run_trading_cycle()
    elif args.command == "schedule":
        start_scheduler()
    elif args.command == "api":
        start_api()
    elif args.command == "status":
        print_status()


if __name__ == "__main__":
    main()
