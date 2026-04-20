"""
api/routes.py — FastAPI REST endpoints.

Start the server with:
    uvicorn api.routes:app --reload --port 8000

Endpoints
─────────
GET  /health              — liveness check
GET  /portfolio           — current Alpaca account + open positions
GET  /signals             — most recent signals from the DB
GET  /trades              — trade history
GET  /portfolio/history   — portfolio equity snapshots
POST /run                 — manually trigger a full trading cycle
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException

import config
from data import fetcher
from db import storage
from signals import generator
from agent.trader import TradingAgent

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Swing Trader API",
    description="Automated swing trading system for US equities (paper trading).",
    version="1.0.0",
)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ── Portfolio ──────────────────────────────────────────────────────────────────

@app.get("/portfolio")
def portfolio() -> dict:
    """Return the current Alpaca account summary and open positions."""
    try:
        from executor import trade_executor
        acct = trade_executor.get_account()
        positions = trade_executor.get_positions()
        return {"account": acct, "positions": positions}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("Portfolio fetch error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch portfolio.")


@app.get("/portfolio/history")
def portfolio_history(limit: int = 30) -> list[dict]:
    """Return the last `limit` portfolio equity snapshots."""
    snapshots = storage.get_portfolio_history(limit=limit)
    return [
        {
            "id": s.id,
            "created_at": s.created_at.isoformat(),
            "equity": s.equity,
            "cash": s.cash,
            "buying_power": s.buying_power,
            "position_count": s.position_count,
        }
        for s in snapshots
    ]


# ── Signals ────────────────────────────────────────────────────────────────────

@app.get("/signals")
def signals(limit: int = 50) -> list[dict]:
    """Return the most recent signals stored in the DB."""
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
            "macd": r.macd,
            "sma20": r.sma20,
            "sma50": r.sma50,
            "sma200": r.sma200,
            "reasons": r.reasons,
        }
        for r in records
    ]


# ── Trades ─────────────────────────────────────────────────────────────────────

@app.get("/trades")
def trades(limit: int = 50) -> list[dict]:
    """Return recent trade history."""
    records = storage.get_recent_trades(limit=limit)
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "ticker": r.ticker,
            "side": r.side,
            "qty": r.qty,
            "entry_price": r.entry_price,
            "order_id": r.order_id,
            "status": r.status,
            "exit_price": r.exit_price,
            "pnl": r.pnl,
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
        }
        for r in records
    ]


# ── Manual trigger ─────────────────────────────────────────────────────────────

@app.post("/run")
def run_cycle(tickers: list[str] | None = None) -> dict[str, Any]:
    """
    Manually trigger a full trading cycle.

    Optionally pass a list of tickers in the request body; defaults to
    the configured watchlist.

    Example:
        curl -X POST http://localhost:8000/run \
             -H "Content-Type: application/json" \
             -d '["AAPL", "MSFT"]'
    """
    watchlist = tickers or config.WATCHLIST
    logger.info("/run triggered for: %s", watchlist)

    # 1. Fetch market data
    data = fetcher.fetch_ohlcv(watchlist)
    if not data:
        raise HTTPException(status_code=503, detail="Failed to fetch market data.")

    # 2. Generate signals
    signal_results = generator.generate_signals(data)

    # 3. Save signals to DB
    for result in signal_results:
        storage.save_signal(result)

    # 4. Agent decides & executes
    agent = TradingAgent()
    actions = agent.run_cycle(signal_results)

    return {
        "tickers_processed": len(data),
        "signals": [
            {"ticker": s.ticker, "signal": s.signal.value, "score": s.score}
            for s in signal_results
        ],
        "actions": [
            {"ticker": a.ticker, "action": a.action, "reason": a.reason}
            for a in actions
        ],
    }
