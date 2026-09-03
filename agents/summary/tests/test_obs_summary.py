"""Phase 4 — summary reaches parity with research.

`summary_core/summary_logging.py` is a deliberate COPY of research's, not an import: see
the header of either file, and `test_standalone.py`, which exists to keep the
two agents unable to break each other.

This file pins the behaviour changes the port brought with it — two of them to
lines that were already live, so they are called out as changes, not additions.
"""

from __future__ import annotations

import json
import logging

import pytest

from summary_core.summary_logging import (ConsoleFormatter, SummaryLogging, STREAMS,
                              _GLYPHS_UNICODE, configure_logging, current_mode,
                              preview, redact, set_mode, summarize_args)

BIG = "N" * 4000
MULTILINE = "First line of the summary.\nSecond line, which must survive.\n" * 30


@pytest.fixture(autouse=True)
def _clean():
    set_mode("normal")
    root = logging.getLogger("lqabr.summary")
    root.handlers.clear()
    for stream in STREAMS:
        root.getChild(stream).handlers.clear()
    yield
    set_mode("normal")
    root.handlers.clear()
    for stream in STREAMS:
        root.getChild(stream).handlers.clear()


class _Rows(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.rows.append(json.loads(record.getMessage()))
        except ValueError:
            pass


def _obs(name: str):
    logger = logging.getLogger(f"lqabr.test.summary.{name}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Rows()
    logger.addHandler(sink)
    return SummaryLogging(run_id="sum-parity", logger=logger), sink


# --- CHANGED BEHAVIOUR 1: the redactor blanked a credential's NAME --------

def test_a_credential_name_is_printed_not_blanked():
    """`agent_build` emits `secret_name` and `secret_source` on every boot;
    both used to read `<redacted>`, which is the inverse of the rule."""
    clean = redact({"secret_name": "lqabr-anthropic-api-key",
                    "secret_source": "env",
                    "secrets_source": "secret_manager"})
    assert clean["secret_name"] == "lqabr-anthropic-api-key"
    assert clean["secret_source"] == "env"
    assert clean["secrets_source"] == "secret_manager"


def test_a_credential_value_is_still_blanked():
    clean = redact({"api_key": "sk-secret", "authorization": "Bearer x",
                    "hubspot_token": "pat-na1-x", "password": "hunter2"})
    assert set(clean.values()) == {"<redacted>"}


def test_a_credential_nested_in_a_bag_is_still_blanked():
    clean = redact({"params": {"model": "gemini", "api_key": "sk-secret"}})
    assert clean["params"]["model"] == "gemini"
    assert clean["params"]["api_key"] == "<redacted>"


# --- CHANGED BEHAVIOUR 2: service_start moved process -> system -----------

def test_service_start_lands_on_the_system_stream(tmp_path):
    configure_logging("INFO", str(tmp_path), "json")
    SummaryLogging(run_id="sum-boot").system.emit("service_start",
                                                 service="lqabr-summary-agent")
    system = [json.loads(line) for line in
              (tmp_path / "summary_system.log").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    assert [r["event"] for r in system] == ["service_start"]
    process = (tmp_path / "summary_process.log").read_text(encoding="utf-8")
    assert "service_start" not in process, "it used to live here"


# --- the step frame, ported ----------------------------------------------

def test_a_step_left_by_an_exception_still_closes_and_names_it():
    obs, sink = _obs("boom")
    with pytest.raises(ValueError):
        with obs.step("summarize", chars=900):
            raise ValueError("the provider exploded")
    closed = [r for r in sink.rows if r["event"] == "step_out"][-1]
    assert closed["status"] == "failed"
    assert "the provider exploded" in closed["reason"]
    assert closed["duration_ms"] is not None


def test_a_step_reports_what_it_produced():
    obs, sink = _obs("ok")
    with obs.step("fetch", source_ref="https://spring.io/blog") as step:
        step.ok(title="This Week in Spring", chars=1261)
    opened = [r for r in sink.rows if r["event"] == "step_in"][-1]
    closed = [r for r in sink.rows if r["event"] == "step_out"][-1]
    assert opened["source_ref"] == "https://spring.io/blog"
    assert closed["status"] == "ok" and closed["chars"] == 1261


def test_a_step_can_report_that_nothing_needed_doing():
    obs, sink = _obs("skip")
    with obs.step("write_summary", object_id="") as step:
        step.skipped("no hubspot target was supplied")
    closed = [r for r in sink.rows if r["event"] == "step_out"][-1]
    assert closed["status"] == "skipped"
    assert "no hubspot target" in closed["reason"]


# --- phase 2's assertions, now against summary_core ----------------------

def test_the_mode_axis_is_present_and_ordered():
    lengths = {}
    for mode in ("terse", "normal", "debug"):
        set_mode(mode)
        lengths[mode] = len(preview(MULTILINE))
    assert lengths["terse"] == 0
    assert 0 < lengths["normal"] < lengths["debug"]


def test_debug_gives_back_the_argument_itself():
    set_mode("debug")
    assert summarize_args({"summary": BIG})["summary"] == BIG
    assert preview(MULTILINE) == MULTILINE


@pytest.mark.parametrize("mode", ["terse", "normal", "debug"])
@pytest.mark.parametrize("width", [90, 120, 165])
def test_no_console_line_exceeds_the_width_in_any_mode(mode, width):
    set_mode(mode)
    logger = logging.getLogger(f"lqabr.test.summary.w{mode}{width}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)

    class _Sink(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.setFormatter(ConsoleFormatter(colour=False,
                                               glyphs=_GLYPHS_UNICODE, width=width))
            self.lines: list = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(self.format(record))

    sink = _Sink()
    logger.addHandler(sink)
    SummaryLogging(run_id="sum-w", logger=logger).process.emit(
        "model_call", model="gemini-2.5-flash", attempt=1,
        params={"temperature": 0.2, "max_tokens": 2000},
        summary_preview=preview(MULTILINE))
    for rendered in sink.lines:
        for line in rendered.split("\n"):
            assert len(line) <= width, f"{len(line)} > {width}: {line!r}"


def test_a_record_holding_a_newline_is_still_one_json_line():
    set_mode("debug")
    obs, sink = _obs("newline")
    obs.process.emit("model_output_ok", summary_preview=preview(MULTILINE))
    written = json.dumps(sink.rows[-1])
    assert "\n" not in written
    assert "\n" in sink.rows[-1]["summary_preview"], "and survives the round trip"


def test_an_unknown_mode_falls_back_rather_than_raising():
    set_mode("loud")
    assert current_mode() == "normal"


# --- the reason the duplication exists is written down -------------------

def test_both_obs_files_say_why_they_are_copies():
    from pathlib import Path
    here = Path(__file__).resolve().parents[1] / "packages" / "summary_core" / "summary_logging.py"
    there = (Path(__file__).resolve().parents[2] / "research" / "packages"
             / "research_core" / "research_logging.py")
    for path in (here, there):
        text = path.read_text(encoding="utf-8")
        assert "deliberate copy" in text, f"{path.name} does not say why"
        assert "test_standalone" in text, f"{path.name} does not name the guard"
