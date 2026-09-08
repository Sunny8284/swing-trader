"""
agent/reasoner.py — Groq AI plain-English reasoning for trade signals.

Model name lives in config.GROQ_MODEL. Groq retires hosted models without
notice, so treat it as configuration, not a constant.
"""

import os
import time
import logging

from groq import Groq

import config

PACING_SECONDS = 1.0

logger = logging.getLogger(__name__)

_client: Groq | None = None

# Set once the configured model is known to be unreachable. A bad model name
# fails identically for every ticker, and the old code retried all 27 of them
# each cycle — 27 log lines and 27 seconds of pacing sleep to accomplish
# nothing. Stop after the first definitive failure.
_model_unavailable = False


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
    global _model_unavailable

    client = _get_client()
    if client is None or _model_unavailable:
        return ""

    reasons = result.reasons if isinstance(result.reasons, list) else []

    user_msg = (
        f"Ticker: {result.ticker}\n"
        f"Signal: {result.signal.value}  Score: {result.score:+d}\n"
        f"Price: ${result.price:.2f}  RSI: {result.rsi:.1f}\n"
        f"SMA20: ${result.sma20:.2f}  SMA50: ${result.sma50:.2f}"
        + (f"  SMA200: ${result.sma200:.2f}" if result.sma200 is not None else "") + "\n"
        f"BB Upper: ${result.bb_upper:.2f}  BB Lower: ${result.bb_lower:.2f}\n"
        f"Reasons: {' | '.join(reasons)}\n\n"
        f"Explain this signal."
    )

    try:
        response = client.chat.completions.create(
            model=config.GROQ_MODEL,
            # 200 was cutting explanations off mid-sentence.
            max_tokens=320,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        # A missing model is a configuration fault, not a transient blip: it
        # will fail for every ticker of every cycle until someone changes it.
        # Log it as an error and stop retrying, so it cannot rot unnoticed
        # behind a wall of per-ticker warnings.
        if "model_not_found" in str(e) or "does not exist" in str(e):
            _model_unavailable = True
            logger.error(
                "Groq model %r is unavailable — AI reasoning disabled for this run. "
                "Set GROQ_MODEL in .env to a model your key can reach.",
                config.GROQ_MODEL,
            )
        else:
            logger.warning("Reasoner failed for %s: %s", result.ticker, e)
        return ""
    finally:
        time.sleep(PACING_SECONDS)
