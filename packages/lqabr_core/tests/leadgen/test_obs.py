"""Observability — four logs, keyed by run_id + lead_ref_id."""

from __future__ import annotations

import io
import json

from lqabr_core.obs import LOG_TYPES, Observability, RunContext, new_lead_ref_id, new_run_id


def _capture() -> tuple[Observability, io.StringIO]:
    stream = io.StringIO()
    return Observability(RunContext(run_id="run-123"), stream=stream), stream


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_four_streams_exist():
    obs, _ = _capture()
    assert LOG_TYPES == ("system", "process", "audit", "tokens")
    for name in LOG_TYPES:
        assert hasattr(obs, name)


def test_every_line_carries_the_run_id():
    obs, stream = _capture()
    obs.system.emit("container_up")
    obs.process.emit("step2_start")
    obs.audit.emit("hubspot_call", endpoint="/crm/v3/objects/contacts", status=200)
    assert [line["run_id"] for line in _lines(stream)] == ["run-123"] * 3


def test_lead_lines_carry_the_lead_ref_id():
    obs, stream = _capture()
    ref = new_lead_ref_id()
    obs.process.emit("lead_dispatch", lead_ref_id=ref)
    assert _lines(stream)[0]["lead_ref_id"] == ref


def test_token_stream_is_wired_but_silent_for_this_agent():
    obs, stream = _capture()
    assert obs.tokens.enabled is False
    assert obs.tokens.emit("model_call", input_tokens=10) is None
    assert _lines(stream) == []


def test_token_stream_can_be_enabled_for_model_agents():
    stream = io.StringIO()
    obs = Observability(RunContext(run_id="run-9"), tokens_enabled=True, stream=stream)
    obs.tokens.emit("model_call", input_tokens=10, output_tokens=4)
    line = _lines(stream)[0]
    assert line["log"] == "tokens"
    assert line["input_tokens"] == 10


def test_timed_audit_records_duration_and_status():
    obs, stream = _capture()
    with obs.timed_audit("hubspot_search_contacts", endpoint="/search", method="POST") as timer:
        timer.extra["status"] = 200
    line = _lines(stream)[0]
    assert line["log"] == "audit"
    assert line["status"] == 200
    assert line["outcome"] == "ok"
    assert "duration_ms" in line


def test_timed_audit_reports_errors_without_swallowing_them():
    obs, stream = _capture()
    try:
        with obs.timed_audit("hubspot_call", endpoint="/x"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    line = _lines(stream)[0]
    assert line["outcome"] == "error"
    assert "boom" in line["error"]


def test_system_snapshot_reports_resource_use():
    obs, stream = _capture()
    obs.system_snapshot("step2_memory_before")
    line = _lines(stream)[0]
    assert line["log"] == "system"
    assert any(key in line for key in ("rss_mb", "max_rss_mb", "resource_probe"))


def test_ids_are_unique():
    assert new_run_id() != new_run_id()
    assert new_lead_ref_id() != new_lead_ref_id()


def test_logging_never_raises():
    obs, _ = _capture()

    class Unserialisable:
        def __repr__(self):
            return "<obj>"

    assert obs.process.emit("weird", payload=Unserialisable()) is not None
