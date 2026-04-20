"""
agent/reasoner.py — Claude AI plain-English reasoning for trade signals.

Uses prompt caching on the system prompt (identical across all tickers each cycle)
to keep API costs low when processing 18 tickers per run.
"""

import os
import logging

import anthropic

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

_SYSTEM = """You are a concise swing-trading analyst. Given technical indicator data for a US equity, write 2-3 sentences explaining the signal in plain English that a retail investor can understand. Focus on the most important factors driving the BUY/SELL/HOLD decision. Be direct — no disclaimers, no caveats, no repetition of raw numbers unless critical. End with one forward-looking sentence about what to watch."""


def explain(result) -> str:
    """
    Call Claude to generate a plain-English explanation for a signal result.
    Returns the explanation string, or empty string on failure.
    """
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
        response = _client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.warning("Reasoner failed for %s: %s", result.ticker, e)
        return ""
