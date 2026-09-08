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
from datetime import date
from typing import Optional

import pandas as pd
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
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


def is_trading_day(day: Optional[date] = None) -> bool:
    """
    Return True if `day` (default today) is a session on the US market calendar.

    Weekday checks are not enough: Labor Day 2026-09-07 was a Monday, the
    scheduler fired all three cycles into a closed market, and every resulting
    order queued unfilled overnight. We ask Alpaca for the calendar rather than
    hardcoding a holiday list.

    Fails CLOSED — if the calendar cannot be reached we report "not a trading
    day" and skip the cycle, because the cost of skipping a session is far
    lower than the cost of trading blind into a closed one.
    """
    day = day or date.today()
    client = _get_client()
    try:
        sessions = client.get_calendar(GetCalendarRequest(start=day, end=day))
    except Exception as exc:
        logger.error("Could not fetch market calendar for %s: %s — treating as closed.", day, exc)
        return False

    open_today = any(s.date == day for s in sessions)
    if not open_today:
        logger.info("%s is not a trading session (weekend or market holiday).", day)
    return open_today


def has_pending_buy(ticker: str) -> bool:
    """
    Return True if an unfilled BUY order for `ticker` is already resting.

    has_position() alone is not a sufficient duplicate guard. An order that has
    not filled yet creates no position, so consecutive cycles each see "no
    position" and stack another order — which is exactly how one COST signal
    became three orders and a 24% position. Sell orders are ignored: entries
    now attach a protective stop, and that leg must not block a re-entry.
    """
    client = _get_client()
    try:
        orders = client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker])
        )
    except Exception as exc:
        # Fail closed: unsure means do not add another order.
        logger.error("Could not check pending orders for %s: %s — assuming one exists.", ticker, exc)
        return True

    for order in orders:
        if order.side == OrderSide.BUY:
            logger.info("Pending unfilled BUY already resting for %s (order %s).", ticker, order.id)
            return True
    return False


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

    # VIX-based position sizing — trade smaller when market fear is elevated
    position_pct = config.MAX_POSITION_PCT
    if config.VIX_SIZING:
        try:
            vix_df = yf.download("^VIX", period="2d", progress=False, auto_adjust=True)
            vix = float(vix_df["Close"].squeeze().iloc[-1])
            if vix >= config.VIX_HIGH_THRESHOLD:
                position_pct = config.VIX_HIGH_POSITION_PCT
                logger.info("VIX=%.1f (≥%.0f) — using reduced position size %.0f%%", vix, config.VIX_HIGH_THRESHOLD, position_pct * 100)
            else:
                logger.debug("VIX=%.1f — normal position size %.0f%%", vix, position_pct * 100)
        except Exception as exc:
            logger.warning("Could not fetch VIX — using default position size: %s", exc)

    # How much cash we're willing to deploy for this position
    max_spend = equity * position_pct
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

    stop_loss_price = stop_price_for(ticker, price)

    logger.info(
        "BUY %s: %d shares @ ~%.2f (total ≈ $%.0f) | SL %.2f (trailing exit owned by agent)",
        ticker, qty, price, qty * price, stop_loss_price,
    )

    client = _get_client()
    order_request = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        # OTO, not BRACKET. A bracket also needs a take-profit leg, and since
        # the trailing stop became the real profit exit, TAKE_PROFIT_PCT was set
        # to 50% to neutralise it — leaving a resting sell limit far above the
        # market on every position for the sole purpose of never filling. OTO
        # attaches the protective stop and nothing else.
        order_class=OrderClass.OTO,
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


def stop_price_for(ticker: str, entry_price: float) -> float:
    """
    Stop for a new entry, scaled to how much `ticker` actually moves.

    A flat percentage stop treats a 3%-a-day name (NVDA) and a 1.8%-a-day name
    (COST) identically, which means it is either far too tight for one or too
    loose for the other. At config.STOP_LOSS_PCT (1.5%) it was inside a single
    day's normal range for every stock on the watchlist, so it fired on noise.

    Falls back to the flat percentage if ATR cannot be computed, and is always
    clamped to [ATR_STOP_MIN_PCT, ATR_STOP_MAX_PCT] so a data glitch cannot
    produce an absurd stop.
    """
    stop_pct = config.STOP_LOSS_PCT
    try:
        bars = yf.download(
            ticker, period="3mo", progress=False, auto_adjust=True, threads=False
        )
        if len(bars) > config.ATR_STOP_PERIOD:
            high = bars["High"].squeeze()
            low = bars["Low"].squeeze()
            close = bars["Close"].squeeze()
            prev_close = close.shift(1)
            true_range = pd.concat([
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ], axis=1).max(axis=1)
            atr = true_range.rolling(config.ATR_STOP_PERIOD).mean().iloc[-1]
            atr_pct = float(atr) / float(close.iloc[-1])
            if atr_pct > 0:
                stop_pct = config.ATR_STOP_MULT * atr_pct
        else:
            logger.warning("Not enough bars for ATR on %s — using flat stop.", ticker)
    except Exception as exc:
        logger.warning("ATR lookup failed for %s (%s) — using flat stop.", ticker, exc)

    stop_pct = max(config.ATR_STOP_MIN_PCT, min(config.ATR_STOP_MAX_PCT, stop_pct))
    logger.info("%s stop width %.2f%% (%.1fx ATR)", ticker, stop_pct * 100, config.ATR_STOP_MULT)
    return round(entry_price * (1 - stop_pct), 2)


def rebalance_cash_sleeve() -> Optional[dict]:
    """
    Hold config.PARK_TARGET_PCT of idle cash in config.PARK_IDLE_CASH_IN.

    Idle cash is the drag that kept this account at +2% while SPY made +16.9%.
    The sleeve is a passive core: plain market orders, no stop, no trailing
    exit. The swing strategy runs on top of it.

    Rebalances only past PARK_DRIFT_PCT of drift — a daily round-trip on the
    whole sleeve costs two-way slippage 250 times a year, which in backtest
    turned +18.9% into +2.6%.
    """
    park = config.PARK_IDLE_CASH_IN
    if not park:
        return None

    client = _get_client()
    try:
        acct = client.get_account()
        cash = float(acct.cash or 0)
        positions = {p.symbol: p for p in client.get_all_positions()}
    except Exception as exc:
        logger.error("Cash sleeve: could not read account: %s", exc)
        return None

    held = positions.get(park)
    held_value = float(held.market_value) if held else 0.0
    price = float(held.current_price) if held else _last_price(park)
    if not price:
        logger.error("Cash sleeve: no price for %s — skipping.", park)
        return None

    idle = cash + held_value
    target_value = idle * config.PARK_TARGET_PCT
    delta_value = target_value - held_value

    if abs(delta_value) < idle * config.PARK_DRIFT_PCT:
        logger.info("Cash sleeve: %s within drift band — no action.", park)
        return None

    qty = int(abs(delta_value) / price)
    if qty < 1:
        return None

    side = OrderSide.BUY if delta_value > 0 else OrderSide.SELL
    logger.info(
        "Cash sleeve: %s %d %s (held $%.0f -> target $%.0f of $%.0f idle)",
        side.value.upper(), qty, park, held_value, target_value, idle,
    )
    try:
        order = client.submit_order(order_data=MarketOrderRequest(
            symbol=park, qty=qty, side=side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
        ))
        return {"ticker": park, "side": side.value, "qty": qty, "order_id": str(order.id)}
    except Exception as exc:
        logger.error("Cash sleeve: %s order failed: %s", park, exc)
        return None


def _last_price(ticker: str) -> float:
    try:
        bars = yf.download(ticker, period="5d", progress=False, auto_adjust=True, threads=False)
        return float(bars["Close"].squeeze().iloc[-1])
    except Exception:
        return 0.0


def cancel_open_orders(ticker: str) -> int:
    """
    Cancel every open order for `ticker`. Returns the number cancelled.

    Exits are agent-driven (trailing stop), but each entry also leaves a
    protective stop resting at the broker. Whenever we close a position
    ourselves, that stop has to go with it.
    """
    client = _get_client()
    try:
        orders = client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker])
        )
    except Exception as exc:
        logger.error("Could not list open orders for %s: %s", ticker, exc)
        return 0

    cancelled = 0
    for order in orders:
        try:
            client.cancel_order_by_id(order.id)
            cancelled += 1
            logger.info("Cancelled resting %s order %s for %s", order.order_type, order.id, ticker)
        except Exception as exc:
            # Already filled or cancelled is fine; anything else we want to see.
            logger.warning("Could not cancel order %s for %s: %s", order.id, ticker, exc)

    if cancelled:
        logger.info("Cancelled %d resting order(s) for %s before selling.", cancelled, ticker)
    return cancelled


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

    # Cancel the protective stop FIRST. close_position() submits an independent
    # market sell; it does not retire the stop attached at entry. Leaving that
    # stop resting against a position we just liquidated means a later trigger
    # sells shares we no longer own — i.e. opens an unintended short.
    cancel_open_orders(ticker)

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
