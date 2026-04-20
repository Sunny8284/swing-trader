"""
agent/reasoner.py — Groq AI plain-English reasoning for trade signals.

Uses llama-3.1-8b-instant on Groq's free tier — fast (~200ms) and zero cost.
"""

import os
import logging

from groq import Groq

logger = logging.getLogger(__name__)

_client: Groq | None = None


def _get_client() -> Groq | None:
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None
    global _client
    if _client is None:
        _client = Groq(api_key=key)
    return _client


_SYSTEM = (
    "You are a concise swing-trading analyst. Given technical indicator data for a US equity, "
    "write 2-3 sentences explaining the signal in plain English that a retail investor can understand. "
    "Focus on the most important factors driving the BUY/SELL/HOLD decision. "
    "Be direct — no disclaimers, no caveats, no repetition of raw numbers unless critical. "
    "End with one forward-looking sentence about what to watch."
)


def explain(result) -> str:
    """
    Call Groq to generate a plain-English explanation for a signal result.
    Returns the explanation string, or empty string on failure / no key set.
    """
    client = _get_client()
    if client is None:
        return ""

    reasons = result.reasons if isinstance(result.reasons, list) else []

    user_msg = (
        f"Ticker: {result.ticker}\n"
        f"Signal: {result.signal.value}  Score: {result.score:+d}\n"
        f"Price: ${result.price:.2f}  RSI: {result.rsi:.1f}\n"
        f"SMA20: ${result.sma20:.2f}  SMA50: ${result.sma50:.2f}  SMA200: ${result.sma200:.2f}\n"
        f"BB Upper: ${result.bb_upper:.2f}  BB Lower: ${result.bb_lower:.2f}\n"
        f"Reasons: {' | '.join(reasons)}\n\n"
        f"Explain this signal."
    )

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=200,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("Reasoner failed for %s: %s", result.ticker, e)
        return ""
