"""
tests/test_reasoner.py — Groq reasoning failure handling.

Groq retired llama-3.1-8b-instant and every reasoning call 404'd for months
without anyone noticing: the failure was caught, logged at WARNING once per
ticker, and the cycle carried on placing trades with no reasoning attached.
"""

import logging

import pytest

from agent import reasoner
from signals.generator import Signal, SignalResult


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(reasoner, "_model_unavailable", False)
    monkeypatch.setattr(reasoner, "PACING_SECONDS", 0)


@pytest.fixture
def signal():
    r = SignalResult(ticker="COST", signal=Signal.BUY, score=2, price=915.74,
                     rsi=38.1, sma20=947.19, sma50=943.01,
                     bb_upper=980.0, bb_lower=919.46)
    r.reasons = ["bullish regime"]
    return r


class FakeCompletions:
    def __init__(self, exc=None, text="looks good."):
        self.exc = exc
        self.text = text
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.exc:
            raise self.exc
        msg = type("M", (), {"content": self.text})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


def _client_with(completions):
    chat = type("Chat", (), {"completions": completions})
    return type("Client", (), {"chat": chat})


def test_missing_model_is_logged_as_error(monkeypatch, signal, caplog):
    """A dead model must not hide at WARNING — that is how it rotted."""
    exc = Exception("Error code: 404 - model_not_found: does not exist")
    monkeypatch.setattr(reasoner, "_get_client", lambda: _client_with(FakeCompletions(exc)))

    with caplog.at_level(logging.ERROR):
        assert reasoner.explain(signal) == ""

    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_missing_model_stops_retrying(monkeypatch, signal):
    """One bad model name should not cost 27 failed calls per cycle."""
    comp = FakeCompletions(Exception("Error code: 404 - model_not_found"))
    monkeypatch.setattr(reasoner, "_get_client", lambda: _client_with(comp))

    for _ in range(5):
        reasoner.explain(signal)

    assert comp.calls == 1, f"kept calling a known-dead model ({comp.calls} times)"


def test_transient_error_keeps_trying(monkeypatch, signal):
    """A timeout is not a config fault — do not disable reasoning for the run."""
    comp = FakeCompletions(Exception("connection reset"))
    monkeypatch.setattr(reasoner, "_get_client", lambda: _client_with(comp))

    for _ in range(3):
        reasoner.explain(signal)

    assert comp.calls == 3


def test_uses_configured_model(monkeypatch, signal):
    import config
    seen = {}

    class Recorder(FakeCompletions):
        def create(self, **kwargs):
            seen["model"] = kwargs["model"]
            return super().create(**kwargs)

    monkeypatch.setattr(config, "GROQ_MODEL", "test/model-x")
    monkeypatch.setattr(reasoner, "_get_client", lambda: _client_with(Recorder()))

    reasoner.explain(signal)

    assert seen["model"] == "test/model-x"
