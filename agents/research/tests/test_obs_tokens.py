"""Phase 3 — the token fold.

The FR names a fourth log, `tokens`, wherever a model runs. It folds onto the
audit hop instead of becoming a fourth file, on one dividing line:

    audit records what the call COST; process records what it PRODUCED.

So counts ride the hop, and `stop_reason` / `searches` / `chars` do not. The
acceptance list is §6 of claude/LOG_DESIGN_2026-08-26.md.
"""

from __future__ import annotations

import json
import logging

import pytest

from research_core.obs import Observability, redact, set_mode
from research_core.search.anthropic_search import AnthropicWebSearch
from research_core.settings import get_settings

COUNTS = ("input_tokens", "output_tokens", "web_search_requests")


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

    def event(self, name: str) -> dict:
        found = [r for r in self.rows if r.get("event") == name]
        assert found, f"no {name}; saw {[r.get('event') for r in self.rows]}"
        return found[-1]


def _obs(name: str):
    logger = logging.getLogger(f"lqabr.test.tokens.{name}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Rows()
    logger.addHandler(sink)
    return Observability(run_id="res-tokens", logger=logger), sink


class _Messages:
    def __init__(self, usage) -> None:
        self._usage = usage

    def create(self, **payload):
        if self._usage == "raise":
            raise RuntimeError("overloaded_error")
        reply = {"content": [{"type": "text", "text": "A grounded note."}],
                 "stop_reason": "end_turn"}
        if self._usage:
            reply["usage"] = self._usage
        return reply


class _Fake:
    def __init__(self, usage=None) -> None:
        self.messages = _Messages(usage)


def _research(name: str, usage):
    obs, sink = _obs(name)
    provider = AnthropicWebSearch(settings=get_settings(refresh=True), obs=obs,
                                  client=_Fake(usage), api_key="test-only")
    return provider, sink


@pytest.fixture(autouse=True)
def _normal_mode():
    set_mode("normal")
    yield
    set_mode("normal")


FULL = {"input_tokens": 16743, "output_tokens": 573,
        "server_tool_use": {"web_search_requests": 2}}


def test_a_successful_call_puts_all_three_counts_on_the_audit_hop():
    provider, sink = _research("ok", FULL)
    provider.research("prompt")
    hop = sink.audit()[-1]
    assert hop["service"] == "anthropic" and hop["endpoint"] == "messages.create"
    assert (hop["input_tokens"], hop["output_tokens"],
            hop["web_search_requests"]) == (16743, 573, 2)


def test_a_call_that_raises_emits_an_error_hop_with_no_token_fields():
    """A call that raised produced no usage. Asserted, not left ambiguous."""
    provider, sink = _research("raise", "raise")
    with pytest.raises(Exception):
        provider.research("prompt")
    hop = sink.audit()[-1]
    assert hop["error"] and hop["status"] is None
    assert not [name for name in COUNTS if name in hop]


def test_usage_absent_from_the_reply_degrades_to_a_hop_with_no_counts():
    provider, sink = _research("nousage", None)
    provider.research("prompt")          # must not raise
    hop = sink.audit()[-1]
    assert hop["status"] == 200
    assert not [name for name in COUNTS if name in hop]


def test_the_outcome_fields_stay_on_process():
    """The fold must not drag what the call PRODUCED along with what it cost."""
    provider, sink = _research("split", FULL)
    provider.research("prompt")
    produced = sink.event("model_response")
    for field in ("stop_reason", "searches", "sources", "chars"):
        assert field in produced, f"{field} belongs on process"
    assert "stop_reason" not in sink.audit()[-1]


def test_the_counts_are_kept_on_process_for_one_more_release():
    """Migrate, don't yank: pulling a field out of a line something may
    already read is the same class of change as re-homing service_start."""
    provider, sink = _research("migrate", FULL)
    provider.research("prompt")
    produced = sink.event("model_response")
    assert produced["input_tokens"] == 16743
    assert produced["output_tokens"] == 573


@pytest.mark.parametrize("mode", ["terse", "normal", "debug"])
def test_a_token_count_still_survives_redaction_on_audit(mode):
    set_mode(mode)
    clean = redact({"input_tokens": 16743, "output_tokens": 573,
                    "web_search_requests": 2, "api_key": "sk-ant-x"})
    assert clean["input_tokens"] == 16743
    assert clean["output_tokens"] == 573
    assert clean["web_search_requests"] == 2
    assert clean["api_key"] == "<redacted>"


def test_the_meter_rides_the_line_for_the_call_it_measures():
    """`_usage()` used to run after the hop was written, so answering "what did
    that call cost" meant joining audit to process on run_id and ordering."""
    provider, sink = _research("order", FULL)
    provider.research("prompt")
    events = [r.get("event") for r in sink.rows]
    hop_at = events.index("outbound_call")
    response_at = events.index("model_response")
    assert hop_at < response_at
    assert sink.audit()[-1]["input_tokens"] == 16743, "already on the hop"


@pytest.mark.parametrize("mode", ["terse", "normal", "debug"])
def test_the_console_hop_line_says_what_the_call_cost(mode):
    """The counts rode the JSON record from the start, but the text renderer
    never printed them — so the one place a human asks "what did that cost"
    was the one place the answer was missing. Terse included: terse drops the
    params, not the meter."""
    from research_core.obs import ConsoleFormatter  # noqa: WPS433 - local on purpose

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
    from research_core.obs import ConsoleFormatter

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
