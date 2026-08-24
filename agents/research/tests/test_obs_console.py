"""The console is for a person; the file and production stay machine-readable.

Everything here guards one rule: making a log line readable must never cost a
structured field, and must never be the thing that raises.
"""

from __future__ import annotations

import json
import logging

from research_core.obs import (ConsoleFormatter, Observability, _glyphs_for,
                               _GLYPHS_ASCII, _GLYPHS_UNICODE, _terminal_width,
                               configure_logging)


class _Sink(logging.Handler):
    def __init__(self, formatter):
        super().__init__()
        self.setFormatter(formatter)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def _render(colour=False, glyphs=_GLYPHS_UNICODE, **event):
    logger = logging.getLogger(f"lqabr.test.{len(event)}.{event.get('_n', 0)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Sink(ConsoleFormatter(colour=colour, glyphs=glyphs))
    logger.addHandler(sink)
    obs = Observability(run_id="res-test", logger=logger)
    obs.process.emit(event.pop("event"), **{k: v for k, v in event.items()
                                            if not k.startswith("_")})
    return sink.lines[-1]


def test_one_event_renders_as_one_line():
    line = _render(event="campaign_blog_read", object_id="330008697562",
                   blog_industry="FINANCIAL_SERVICES", summary_chars=1261)
    assert "\n" not in line
    assert "campaign_blog_read" in line
    assert "blog_industry=FINANCIAL_SERVICES" in line


def test_empty_fields_are_not_printed():
    """Noise is the whole complaint — a blank value earns no column."""
    line = _render(event="campaign_start", object_id="1", industry_override="",
                   leads=[], limit=100)
    assert "industry_override" not in line
    assert "leads=" not in line
    assert "limit=100" in line


def test_a_failure_is_marked_differently_from_a_success():
    bad = _render(event="run_failed", step="research", reason="no model key")
    good = _render(event="context_write_ok", object_id="1", chars=3941)
    assert _GLYPHS_UNICODE["bad"] in bad
    assert _GLYPHS_UNICODE["ok"] in good


def test_a_long_list_is_summarised_not_dumped():
    line = _render(event="campaign_leads_found", leads=[str(n) for n in range(20)])
    assert "+17" in line and len(line) < 200


def test_secrets_stay_redacted_in_the_readable_form():
    """The console must not become a way to read a value the JSON hides."""
    line = _render(event="secret_resolved", secret="hs-token-abc123",
                   source="secret_manager:proj/name")
    assert "abc123" not in line
    assert "<redacted>" in line
    assert "secret_manager:proj/name" in line


def test_an_outbound_call_reads_as_a_call():
    logger = logging.getLogger("lqabr.test.hop")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Sink(ConsoleFormatter(colour=False, glyphs=_GLYPHS_UNICODE))
    logger.addHandler(sink)
    Observability(run_id="res-test", logger=logger).hop(
        service="hubspot", endpoint="/crm/v3/objects/companies/search",
        status=200, duration_ms=677.1)
    line = sink.lines[-1]
    assert "hubspot" in line and "200" in line and "677ms" in line


def test_a_message_not_from_a_stream_is_passed_through():
    """A stray library warning must survive, not be swallowed by the parser."""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "raw warning", (), None)
    assert ConsoleFormatter(colour=False).format(record) == "raw warning"


def test_a_console_that_cannot_encode_the_glyphs_gets_ascii():
    """A cp1252 Windows console must not turn a log line into a crash."""
    class Cp1252:
        encoding = "cp1252"

    class Utf8:
        encoding = "utf-8"

    assert _glyphs_for(Cp1252()) is _GLYPHS_ASCII
    assert _glyphs_for(Utf8()) is _GLYPHS_UNICODE
    assert _glyphs_for(object()) is _GLYPHS_ASCII      # no encoding attribute


def test_the_log_file_stays_json_while_the_console_is_text(tmp_path):
    """The readable form is a console concern only — the file is still parsed."""
    path = tmp_path / "agent.log"
    logging.getLogger("lqabr.research").handlers.clear()
    configure_logging("INFO", str(path), "text")
    Observability(run_id="res-test").process.emit("context_write_ok", chars=3941)
    logging.getLogger("lqabr.research").handlers.clear()

    record = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["event"] == "context_write_ok"
    assert record["stream"] == "process"
    assert record["chars"] == 3941


# --- watching a long run: where am I, and is the line intact? ---------------

def _lines(events, width=165):
    logger = logging.getLogger("lqabr.test.progress")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Sink(ConsoleFormatter(colour=False, glyphs=_GLYPHS_UNICODE, width=width))
    logger.addHandler(sink)
    obs = Observability(run_id="res-test", logger=logger)
    for name, fields in events:
        obs.process.emit(name, **fields)
    return sink.lines


def test_a_lead_line_says_where_it_is_in_the_queue():
    """The complaint this answers: \"I do not know how many are remaining\"."""
    start, done = _lines([
        ("campaign_lead_start", {"object_id": "533970643697", "position": 2, "of": 5}),
        ("campaign_lead_done", {"object_id": "533970643697", "position": 2,
                                "of": 5, "status": "completed", "chars": 3557}),
    ])
    assert "lead 2/5" in start and "working" in start
    assert "lead 2/5" in done and "(3 left)" in done


def test_the_start_line_does_not_count_the_lead_it_is_working():
    """\"5 left\" while working lead 1 of 5 reads as one too many."""
    start = _lines([("campaign_lead_start",
                     {"object_id": "1", "position": 1, "of": 5})])[0]
    assert "left" not in start


def test_a_failed_lead_still_reports_position_and_reason():
    line = _lines([("campaign_lead_done",
                    {"object_id": "1", "position": 4, "of": 5, "status": "failed",
                     "chars": 0, "error": "crm-error: no lead for that id"})])[0]
    assert "lead 4/5" in line and "failed" in line
    assert "crm-error" in line and "(1 left)" in line


def test_no_line_exceeds_the_terminal_width():
    """A wrapped line redraws over its neighbour — that is the log corrupting."""
    events = [("context_write_ok", {"object_id": "533970643697", "chars": 3557,
                                    "property_name": "lead_context",
                                    "note": "x" * 200}),
              ("campaign_leads_found", {"industry": "FINANCIAL_SERVICES",
                                        "leads_found": 5,
                                        "leads": [str(n) for n in range(40)]})]
    for width in (90, 120, 165):
        for line in _lines(events, width=width):
            assert len(line) <= width, f"{len(line)} > {width}: {line!r}"


def test_the_constant_write_note_is_not_printed_to_the_console():
    """It is byte-identical on every lead: noise on screen, kept in the JSON."""
    line = _lines([("context_write_ok",
                    {"object_id": "1", "chars": 3557,
                     "note": "this write raises HubSpot trigger 2"})])[0]
    assert "raises HubSpot trigger" not in line
    assert "chars=3557" in line


def test_terminal_width_is_never_absurdly_narrow():
    assert _terminal_width() >= 90
