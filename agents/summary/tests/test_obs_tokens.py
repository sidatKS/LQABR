"""Phase 3 — the token fold, summary side.

Summary had no model hop at all: `model_call` on process said a call happened,
and nothing recorded that it left the process or what it spent. The same
dividing line applies — audit records what the call COST, process records what
it PRODUCED — and the retry loop means the counts are per-ATTEMPT.
"""

from __future__ import annotations

import json
import logging

import pytest

from summary_core.summary_logging import SummaryLogging, redact, set_mode
from summary_core.settings import get_settings
from summary_core.types import NormalizedDocument

import summarizer

GOOD = json.dumps({"summary": "A grounded summary of the document. " * 6,
                   "key_points": ["one", "two", "three"],
                   "industry": "HEALTHCARE"})
DOC = NormalizedDocument(source_kind="url", source_ref="https://spring.io/blog",
                         text="body text " * 40)
COUNTS = ("input_tokens", "output_tokens", "total_tokens")


class _Rows(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.rows.append(json.loads(record.getMessage()))
        except ValueError:
            pass

    def audit(self) -> list:
        return [r for r in self.rows if r["stream"] == "audit"]


def _run(completion, name: str):
    logger = logging.getLogger(f"lqabr.test.tokens.{name}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Rows()
    logger.addHandler(sink)
    obs = SummaryLogging(run_id="sum-tokens", logger=logger)
    try:
        summarizer.summarize(DOC, settings=get_settings(refresh=True), obs=obs,
                             completion=completion)
    except Exception:  # noqa: BLE001 - some cases fail on purpose
        pass
    return sink


@pytest.fixture(autouse=True)
def _normal_mode():
    set_mode("normal")
    yield
    set_mode("normal")


def _reply(usage=None, body: str = GOOD):
    reply = {"choices": [{"message": {"content": body}}]}
    if usage:
        reply["usage"] = usage
    return lambda **kwargs: reply


def test_the_model_call_now_reaches_the_audit_stream():
    sink = _run(_reply({"prompt_tokens": 1200, "completion_tokens": 240,
                        "total_tokens": 1440}), "hop")
    hops = sink.audit()
    assert hops, "summary had no model hop at all before this"
    assert hops[-1]["service"] == "model"
    assert hops[-1]["method"] == "completion"
    assert hops[-1]["status"] == 200


def test_the_counts_ride_the_hop():
    sink = _run(_reply({"prompt_tokens": 1200, "completion_tokens": 240,
                        "total_tokens": 1440}), "counts")
    hop = sink.audit()[-1]
    assert (hop["input_tokens"], hop["output_tokens"],
            hop["total_tokens"]) == (1200, 240, 1440)


def test_usage_absent_degrades_to_a_hop_with_no_counts_never_a_raise():
    """Summary reaches the model through an injected callable and an ADK path;
    whether the reply carries `usage` is not something it gets to assume."""
    sink = _run(_reply(None), "nousage")
    hop = sink.audit()[-1]
    assert hop["status"] == 200
    assert not [name for name in COUNTS if name in hop]


def test_each_retry_attempt_emits_its_own_hop_with_its_own_counts():
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        body = GOOD if calls["n"] == 2 else "not json at all"
        return {"choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 100 * calls["n"],
                          "completion_tokens": 10}}

    sink = _run(flaky, "retry")
    hops = sink.audit()
    assert [h["attempt"] for h in hops] == [1, 2]
    assert [h["input_tokens"] for h in hops] == [100, 200]


def test_a_provider_that_raises_gets_an_error_hop_with_no_counts():
    def boom(**kwargs):
        raise RuntimeError("provider unavailable")

    hop = _run(boom, "boom").audit()[-1]
    assert hop["status"] is None and "provider unavailable" in hop["error"]
    assert not [name for name in COUNTS if name in hop]


@pytest.mark.parametrize("mode", ["terse", "normal", "debug"])
def test_a_token_count_is_not_a_token(mode):
    """Without the exemption the fold lands `input_tokens: "<redacted>"` on
    every hop, because the hint list matches the substring "token"."""
    set_mode(mode)
    clean = redact({"input_tokens": 1200, "output_tokens": 240,
                    "total_tokens": 1440, "api_key": "sk-secret"})
    assert clean["input_tokens"] == 1200
    assert clean["output_tokens"] == 240
    assert clean["total_tokens"] == 1440
    assert clean["api_key"] == "<redacted>"


@pytest.mark.parametrize("mode", ["terse", "normal", "debug"])
def test_the_console_hop_line_says_what_the_call_cost(mode):
    """The counts rode the JSON record from the start, but the text renderer
    never printed them — so the one place a human asks "what did that cost"
    was the one place the answer was missing. Terse included: terse drops the
    params, not the meter."""
    from summary_core.summary_logging import ConsoleFormatter  # noqa: WPS433 - local on purpose

    set_mode(mode)
    record = logging.LogRecord("x", logging.INFO, __file__, 0, "", None, None)
    record.lqabr_record = {
        "stream": "audit", "event": "outbound_call", "ts": 0,
        "service": "anthropic", "endpoint": "messages.create", "method": "POST",
        "status": 200, "duration_ms": 812.4, "attempt": 1, "error": "",
        "params": {"model": "m"},
        "input_tokens": 4321, "output_tokens": 876, "web_search_requests": 3,
    }
    line = ConsoleFormatter(colour=False, width=200).format(record)
    assert "4321/876 tok" in line
    assert "3srch" in line


def test_a_hop_with_no_meter_prints_no_meter():
    """An MCP call has no tokens; it must not grow an empty `/ tok`."""
    from summary_core.summary_logging import ConsoleFormatter

    set_mode("normal")
    record = logging.LogRecord("x", logging.INFO, __file__, 0, "", None, None)
    record.lqabr_record = {
        "stream": "audit", "event": "outbound_call", "ts": 0, "service": "mcp",
        "endpoint": "http://localhost:8080/mcp", "method": "POST",
        "status": 200, "duration_ms": 2.3, "attempt": 1, "error": "",
        "params": {"method": "initialize"},
    }
    line = ConsoleFormatter(colour=False, width=200).format(record)
    assert "tok" not in line
    assert "srch" not in line
