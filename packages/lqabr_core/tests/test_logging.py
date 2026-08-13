import json
import logging

import pytest

from lqabr_core.logging import correlation_scope, get_agent_logger, log_tool_call


def _capture(logger: logging.Logger):
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(json.loads(self.format(record)))

    handler = _Handler()
    handler.setFormatter(logger.handlers[0].formatter)
    logger.addHandler(handler)
    return records


def test_log_tool_call_emits_process_lines_and_redacts_secrets():
    loggers = get_agent_logger("test_agent")
    process_records = _capture(loggers.process)

    @log_tool_call(loggers)
    def do_thing(email: str, api_key: str = "") -> dict:
        return {"status": "sent"}

    result = do_thing("lead@example.com", api_key="super-secret")
    assert result == {"status": "sent"}

    starts = [r for r in process_records if r.get("event") == "start"]
    assert starts[0]["args"]["api_key"] == "<redacted>"
    assert starts[0]["args"]["email"] == "lead@example.com"
    assert any(r.get("outcome") == "ok" for r in process_records)


def test_log_tool_call_audit_true_emits_audit_line():
    loggers = get_agent_logger("test_agent_audit")
    audit_records = _capture(loggers.audit)

    @log_tool_call(loggers, audit=True, summarize=lambda r: f"sent to {r['to']}")
    def send(to: str) -> dict:
        return {"to": to, "status": "sent"}

    send("lead@example.com")
    assert audit_records[0]["log_type"] == "audit"
    assert audit_records[0]["result"] == "sent to lead@example.com"


def test_log_tool_call_never_swallows_exceptions():
    loggers = get_agent_logger("test_agent_errors")
    system_records = _capture(loggers.system)

    @log_tool_call(loggers)
    def boom() -> None:
        raise RuntimeError("mailgun-error: timeout")

    with pytest.raises(RuntimeError, match="mailgun-error"):
        boom()
    assert system_records[0]["outcome"] == "error"


def test_correlation_scope_tags_every_line_with_same_trace_and_lead():
    loggers = get_agent_logger("test_agent_scope")
    process_records = _capture(loggers.process)

    @log_tool_call(loggers)
    def noop() -> dict:
        return {}

    with correlation_scope(hubspot_contact_id="123"):
        noop()

    assert process_records[0]["hubspot_contact_id"] == "123"
    assert "trace_id" in process_records[0]
