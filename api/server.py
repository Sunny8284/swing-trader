"""
api/server.py — FastAPI dashboard backend.

Exposes Alpaca trading data via REST endpoints for the Next.js frontend.
"""

import os
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import logging

from api.alpaca_client import trading_client
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from db import storage
from db.storage import engine, SessionLocal, TradeRecord
from sqlalchemy import text

import config
from data import fetcher
from signals import generator
from agent.trader import TradingAgent
from agent import reasoner as ai_reasoner
from agent import notifier

logger = logging.getLogger(__name__)

_API_KEY = os.getenv("DASHBOARD_API_KEY", "")  # empty = no auth (local dev)

ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

app = FastAPI(title="Swing Trader Dashboard API", version="1.0.0")

storage.init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """Require X-API-Key header when DASHBOARD_API_KEY is set in env.
    OPTIONS preflight requests are always allowed through for CORS to work."""
    if _API_KEY and request.method != "OPTIONS" and request.url.path != "/api/health":
        key = request.headers.get("X-API-Key", "")
        if key != _API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return await call_next(request)


@app.get("/api/health")
def health():
    """Health check."""
    return {"status": "ok", "mode": "paper"}


@app.get("/api/account")
def get_account():
    """Current cash, buying power, portfolio value."""
    try:
        acct = trading_client.get_account()
        equity = float(acct.equity) if acct.equity else 0
        last_equity = float(acct.last_equity) if acct.last_equity else equity
        
        day_pnl = equity - last_equity
        day_pnl_pct = (day_pnl / last_equity * 100) if last_equity > 0 else 0
        
        return {
            "cash": float(acct.cash) if acct.cash else 0,
            "buying_power": float(acct.buying_power) if acct.buying_power else 0,
            "portfolio_value": float(acct.portfolio_value) if acct.portfolio_value else 0,
            "equity": equity,
            "last_equity": last_equity,
            "day_pnl": day_pnl,
            "day_pnl_pct": day_pnl_pct,
        }
    except Exception as e:
        logger.error(f"Error fetching account: {e}")
        return {
            "cash": 0,
            "buying_power": 0,
            "portfolio_value": 0,
            "equity": 0,
            "last_equity": 0,
            "day_pnl": 0,
            "day_pnl_pct": 0,
        }


@app.get("/api/positions")
def get_positions():
    """All currently open positions."""
    try:
        positions = trading_client.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty) if p.qty else 0,
                "avg_entry_price": float(p.avg_entry_price) if p.avg_entry_price else 0,
                "current_price": float(p.current_price) if p.current_price else 0,
                "market_value": float(p.market_value) if p.market_value else 0,
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0,
                "unrealized_plpc": (float(p.unrealized_plpc) * 100) if p.unrealized_plpc else 0,
            }
            for p in positions
        ]
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
        return []


@app.get("/api/orders")
def get_orders(limit: int = 50):
    """Recent orders (filled, cancelled, etc)."""
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
        orders = trading_client.get_orders(filter=req)
        return [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side.value if o.side else "unknown",
                "qty": float(o.qty) if o.qty else 0,
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                "status": o.status.value if o.status else "unknown",
                "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
                "filled_at": o.filled_at.isoformat() if o.filled_at else None,
            }
            for o in orders
        ]
    except Exception as e:
        logger.error(f"Error fetching orders: {e}")
        return []


@app.get("/api/equity-curve")
def get_equity_curve(days: int = 30):
    """Portfolio history for charting."""
    try:
        # Alpaca's get_portfolio_history uses start/end parameters, not period
        from datetime import datetime, timedelta
        end = datetime.now()
        start = end - timedelta(days=days)
        
        history = trading_client.get_portfolio_history(
            start=start,
            end=end,
            timeframe="1D",
        )
        return {
            "timestamps": [datetime.fromtimestamp(ts).isoformat() for ts in history.timestamp] if history.timestamp else [],
            "equity": list(history.equity) if history.equity else [],
            "profit_loss": list(history.profit_loss) if history.profit_loss else [],
            "profit_loss_pct": list(history.profit_loss_pct) if history.profit_loss_pct else [],
        }
    except Exception as e:
        logger.error(f"Error fetching equity curve: {e}")
        return {
            "timestamps": [],
            "equity": [],
            "profit_loss": [],
            "profit_loss_pct": [],
        }


@app.get("/api/signals")
def get_signals(limit: int = 100):
    """Most recent signals from SQLite — one row per ticker per cycle."""
    try:
        records = storage.get_recent_signals(limit=limit)
        return [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "ticker": r.ticker,
                "signal": r.signal,
                "score": r.score,
                "price": r.price,
                "rsi": r.rsi,
                "sma20": r.sma20,
                "sma50": r.sma50,
                "sma200": r.sma200,
                "bb_upper": r.bb_upper,
                "bb_lower": r.bb_lower,
                "reasons": r.reasons,
                "reasoning": r.reasoning,
            }
            for r in records
        ]
    except Exception as e:
        logger.error(f"Error fetching signals: {e}")
        return []


@app.get("/api/stats")
def get_stats():
    """Win rate and P&L summary from closed trades."""
    try:
        stats = storage.get_trade_stats()
        # Also count total signals generated
        with SessionLocal() as session:
            signal_count = session.execute(text("SELECT COUNT(*) FROM signals")).scalar() or 0
        stats["total_signals"] = signal_count
        return stats
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        return {
            "total_closed": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
            "total_signals": 0,
        }


_backtest_cache: dict  = {}   # {days: (timestamp, result)}
_optimize_cache: dict = {}   # {days: (timestamp, results)}
_BACKTEST_TTL = 3600         # reuse cached result for 1 hour


@app.get("/api/backtest")
def get_backtest(days: int = 365, refresh: bool = False):
    """Run (or return cached) backtest over the last `days` calendar days."""
    import time
    from backtest.engine import run as bt_run

    cached = _backtest_cache.get(days)
    if cached and not refresh and (time.time() - cached[0]) < _BACKTEST_TTL:
        return cached[1]

    try:
        result = bt_run(days=days)
        payload = {
            "start_date":        str(result.start_date),
            "end_date":          str(result.end_date),
            "initial_capital":   result.initial_capital,
            "final_capital":     result.final_capital,
            "total_return_pct":  result.total_return_pct,
            "spy_return_pct":    result.spy_return_pct,
            "total_trades":      result.total_trades,
            "wins":              result.wins,
            "losses":            result.losses,
            "win_rate":          result.win_rate,
            "avg_pnl":           result.avg_pnl,
            "max_drawdown_pct":  result.max_drawdown_pct,
            "sharpe_ratio":      result.sharpe_ratio,
            "equity_curve":      result.equity_curve,
            "trades": [
                {
                    "ticker":       t.ticker,
                    "entry_date":   str(t.entry_date),
                    "exit_date":    str(t.exit_date),
                    "entry_price":  t.entry_price,
                    "exit_price":   t.exit_price,
                    "pnl":          t.pnl,
                    "pnl_pct":      t.pnl_pct,
                    "exit_reason":  t.exit_reason,
                }
                for t in result.trades
            ],
        }
        _backtest_cache[days] = (time.time(), payload)
        return payload
    except Exception as e:
        logger.error(f"Backtest failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/optimize")
def get_optimize(days: int = 365, refresh: bool = False):
    """Grid-search over strategy parameters; returns results ranked by Sharpe."""
    import time
    from backtest.optimizer import run_optimization

    cached = _optimize_cache.get(days)
    if cached and not refresh and (time.time() - cached[0]) < _BACKTEST_TTL:
        return cached[1]

    try:
        results = run_optimization(days=days)
        payload = [
            {
                "buy_threshold":    r.buy_threshold,
                "take_profit_pct":  r.take_profit_pct,
                "stop_loss_pct":    r.stop_loss_pct,
                "total_return_pct": r.total_return_pct,
                "win_rate":         r.win_rate,
                "total_trades":     r.total_trades,
                "sharpe_ratio":     r.sharpe_ratio,
                "max_drawdown_pct": r.max_drawdown_pct,
                "avg_pnl":          r.avg_pnl,
                "is_current":       r.is_current,
            }
            for r in results
        ]
        _optimize_cache[days] = (time.time(), payload)
        return payload
    except Exception as e:
        logger.error(f"Optimization failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _do_cycle(watchlist: list[str]) -> None:
    """Run a full trading cycle in the background. Used by /run."""
    logger.info("Background cycle starting for: %s", watchlist)

    ohlcv = fetcher.fetch_ohlcv(watchlist)
    if not ohlcv:
        logger.error("Failed to fetch market data — aborting cycle.")
        return

    signal_results = generator.generate_signals(ohlcv)

    groq_key = os.environ.get("GROQ_API_KEY", "")
    for result in signal_results:
        record = storage.save_signal(result)
        if groq_key:
            reasoning = ai_reasoner.explain(result)
            if reasoning:
                storage.update_reasoning(record.id, reasoning)
                result._reasoning = reasoning

    agent = TradingAgent()
    actions = agent.run_cycle(signal_results)

    try:
        acct = trading_client.get_account()
        account_data = {
            "portfolio_value": float(acct.portfolio_value or 0),
            "cash": float(acct.cash or 0),
        }
    except Exception:
        account_data = None
    notifier.send_cycle_summary(signal_results, actions, account=account_data)
    logger.info("Background cycle done — %d signals, %d actions.", len(signal_results), len(actions))


@app.post("/run")
def trigger_cycle(background_tasks: BackgroundTasks, tickers: list[str] | None = None):
    """Trigger a full trading cycle. Returns immediately; cycle runs in the background.
    Used by external schedulers (cron-job.org) — must respond well under 30s timeout."""
    watchlist = tickers or config.WATCHLIST
    logger.info("/run accepted for %d tickers — dispatching to background.", len(watchlist))
    background_tasks.add_task(_do_cycle, watchlist)
    return {"status": "accepted", "tickers": len(watchlist)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
