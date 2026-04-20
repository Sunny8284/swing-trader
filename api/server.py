"""
api/server.py — FastAPI dashboard backend.

Exposes Alpaca trading data via REST endpoints for the Next.js frontend.
"""

import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import logging

from api.alpaca_client import trading_client
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from db import storage
from db.storage import engine, SessionLocal, TradeRecord
from sqlalchemy import text

logger = logging.getLogger(__name__)

_API_KEY = os.getenv("DASHBOARD_API_KEY", "")  # empty = no auth (local dev)

ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

app = FastAPI(title="Swing Trader Dashboard API", version="1.0.0")

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
