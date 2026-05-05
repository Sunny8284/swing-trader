"""
executor/trade_executor.py — Submits orders to Alpaca paper trading.

Uses the official alpaca-py SDK. All orders go to the paper trading
endpoint (ALPACA_BASE_URL=https://paper-api.alpaca.markets) so no real
money is ever at risk.

Position sizing
───────────────
For each BUY signal we calculate the number of whole shares that fit
within MAX_POSITION_PCT of current portfolio equity, subject to the
constraint that we never drop below MIN_CASH_RESERVE_PCT in cash.
"""

import logging
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

import config
from db import storage

logger = logging.getLogger(__name__)


def _get_client() -> TradingClient:
    """Return an authenticated Alpaca TradingClient (paper mode)."""
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        raise RuntimeError(
            "Alpaca API credentials not set. "
            "Add ALPACA_API_KEY and ALPACA_SECRET_KEY to your .env file."
        )
    return TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=True,
    )


# ── Portfolio helpers ──────────────────────────────────────────────────────────

def get_account() -> dict:
    """Return key account fields as a plain dict."""
    client = _get_client()
    acct = client.get_account()
    return {
        "equity": float(acct.equity),
        "cash": float(acct.cash),
        "buying_power": float(acct.buying_power),
        "portfolio_value": float(acct.portfolio_value),
        "status": acct.status.value,
    }


def get_positions() -> list[dict]:
    """Return all open positions."""
    client = _get_client()
    positions = client.get_all_positions()
    return [
        {
            "ticker": p.symbol,
            "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc),
        }
        for p in positions
    ]


def has_position(ticker: str) -> bool:
    """Return True if we currently hold shares in `ticker`."""
    try:
        client = _get_client()
        client.get_open_position(ticker)
        return True
    except Exception:
        return False


# ── Order execution ────────────────────────────────────────────────────────────

def execute_buy(ticker: str, price: float) -> Optional[dict]:
    """
    Submit a market BUY order for `ticker`.

    Position size is determined by MAX_POSITION_PCT of portfolio equity,
    capped so that available cash stays above MIN_CASH_RESERVE_PCT.

    Returns a dict with order details, or None if the order was skipped.
    """
    # Skip if we already own this stock
    if has_position(ticker):
        logger.info("Skipping BUY for %s — position already open.", ticker)
        return None

    acct = get_account()
    equity = acct["equity"]
    cash = acct["cash"]

    # How much cash we're willing to deploy for this position
    max_spend = equity * config.MAX_POSITION_PCT
    # Reserve buffer: keep at least MIN_CASH_RESERVE_PCT of equity in cash
    cash_reserve = equity * config.MIN_CASH_RESERVE_PCT
    available = max(0.0, cash - cash_reserve)

    spend = min(max_spend, available)

    if spend < price:
        logger.warning(
            "Insufficient buying power for %s (need %.2f, have %.2f after reserve).",
            ticker, price, spend,
        )
        return None

    qty = int(spend / price)  # whole shares only
    if qty < 1:
        logger.warning("Calculated qty < 1 for %s — skipping.", ticker)
        return None

    take_profit_price = round(price * (1 + config.TAKE_PROFIT_PCT), 2)
    stop_loss_price = round(price * (1 - config.STOP_LOSS_PCT), 2)

    logger.info(
        "BUY %s: %d shares @ ~%.2f (total ≈ $%.0f) | TP %.2f / SL %.2f",
        ticker, qty, price, qty * price, take_profit_price, stop_loss_price,
    )

    client = _get_client()
    order_request = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=take_profit_price),
        stop_loss=StopLossRequest(stop_price=stop_loss_price),
    )

    try:
        order = client.submit_order(order_data=order_request)
        order_id = str(order.id)

        # Persist to DB
        storage.save_trade(
            ticker=ticker,
            side="buy",
            qty=qty,
            entry_price=price,
            order_id=order_id,
            status="submitted",
        )

        logger.info("BUY order submitted for %s — order_id=%s", ticker, order_id)
        return {"ticker": ticker, "side": "buy", "qty": qty, "order_id": order_id}

    except Exception as exc:
        logger.error("Failed to submit BUY order for %s: %s", ticker, exc)
        return None


def execute_sell(ticker: str, price: float) -> Optional[dict]:
    """
    Close the entire position in `ticker` with a market SELL order.

    Returns a dict with order details, or None if no position existed.
    """
    if not has_position(ticker):
        logger.info("Skipping SELL for %s — no open position.", ticker)
        return None

    logger.info("SELL %s: liquidating position @ ~%.2f", ticker, price)

    client = _get_client()

    try:
        # close_position liquidates all shares
        order = client.close_position(ticker)
        order_id = str(order.id)

        # P&L tracking is handled by storage.close_trade() in trader.py
        # — do not save a duplicate sell record here

        logger.info("SELL order submitted for %s — order_id=%s", ticker, order_id)
        return {"ticker": ticker, "side": "sell", "order_id": order_id, "exit_price": price}

    except Exception as exc:
        logger.error("Failed to submit SELL order for %s: %s", ticker, exc)
        return None
