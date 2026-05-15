"""
agent/notifier.py — Telegram notifications for trade signals and executed orders.
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_API = "https://api.telegram.org/bot{token}/sendMessage"


def _send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", _TOKEN)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", _CHAT_ID)
    if not token or not chat_id:
        logger.warning("Telegram not configured — token=%r chat_id=%r", bool(token), bool(chat_id))
        return
    try:
        resp = requests.post(
            _API.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram API error %s: %s", resp.status_code, resp.text[:200])
        else:
            logger.info("Telegram notification sent (HTTP %s)", resp.status_code)
    except Exception as e:
        logger.warning("Telegram notification failed: %s", e)


def send_cycle_summary(signal_results, actions, account=None) -> None:
    """Send a morning summary after each trading cycle."""
    from signals.generator import Signal

    buys = [s for s in signal_results if s.signal.value == "BUY"]
    sells = [s for s in signal_results if s.signal.value == "SELL"]
    executed = [a for a in actions if a.action in ("BUY", "SELL")]

    lines = ["<b>🤖 Swing Trader — Cycle Complete</b>"]

    if account:
        lines.append(
            f"💼 Portfolio: <b>${account.get('portfolio_value', 0):,.2f}</b>  "
            f"Cash: ${account.get('cash', 0):,.2f}"
        )

    # Executed trades
    if executed:
        lines.append("\n<b>✅ Orders Executed:</b>")
        for a in executed:
            emoji = "🟢" if a.action == "BUY" else "🔴"
            lines.append(f"{emoji} <b>{a.action} {a.ticker}</b> — {a.reason}")

    # BUY signals (even if not traded)
    if buys:
        lines.append("\n<b>📈 BUY Signals:</b>")
        for s in sorted(buys, key=lambda x: x.score, reverse=True):
            reasoning = getattr(s, "_reasoning", "")
            line = f"• <b>{s.ticker}</b>  score {s.score:+d}  RSI {s.rsi:.1f}  ${s.price:.2f}"
            lines.append(line)
            if reasoning:
                lines.append(f"  <i>{reasoning}</i>")

    # SELL signals
    if sells:
        lines.append("\n<b>📉 SELL Signals:</b>")
        for s in sorted(sells, key=lambda x: x.score):
            lines.append(f"• <b>{s.ticker}</b>  score {s.score:+d}  ${s.price:.2f}")

    if not buys and not sells and not executed:
        lines.append("\n⏸ All signals HOLD — no trades this cycle.")

    _send("\n".join(lines))


def send_trade_alert(action: str, ticker: str, qty: float, price: float, reasoning: str = "") -> None:
    """Immediate alert when a trade is executed."""
    emoji = "🟢" if action == "BUY" else "🔴"
    text = (
        f"{emoji} <b>{action} {ticker}</b>\n"
        f"Qty: {qty:.0f}  Price: ${price:.2f}\n"
    )
    if reasoning:
        text += f"<i>{reasoning}</i>"
    _send(text)
