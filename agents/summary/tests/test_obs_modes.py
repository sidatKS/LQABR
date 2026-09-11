"""Phase 2 — the terse | normal | debug axis, summary side.

Summary got the ConsoleFormatter in phase 4, so the console assertions live
here too now — the note in this docstring that sent a reader to research's
copy for them was true only before that port. `redact` is unchanged by mode
in one respect and changed in another: debug stops values arriving
pre-mangled, and does NOT relax redaction.
"""

from __future__ import annotations

import logging

import pytest

from summary_core.summary_logging import (ConsoleFormatter, SummaryLogging, current_mode,
                              redact, set_mode)


class _Sink(logging.Handler):
    def __init__(self, width: int = 165) -> None:
        super().__init__()
        self.setFormatter(ConsoleFormatter(colour=False, width=width))
        self.rendered: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.rendered.append(self.format(record))

BIG = "N" * 4000


@pytest.fixture(autouse=True)
def _normal_again():
    yield
    set_mode("normal")


def test_debug_does_not_trim_a_long_value():
    set_mode("debug")
    assert len(redact({"summary": BIG})["summary"]) == 4000


def test_normal_and_terse_still_trim():
    for mode in ("terse", "normal"):
        set_mode(mode)
        trimmed = redact({"summary": BIG})["summary"]
        assert len(trimmed) < 4000
        assert "chars)" in trimmed, "the trim announces itself"


@pytest.mark.parametrize("mode", ["terse", "normal", "debug"])
def test_a_credential_is_redacted_in_every_mode(mode):
    set_mode(mode)
    clean = redact({"api_key": "sk-secret", "authorization": "Bearer x"})
    assert clean["api_key"] == "<redacted>"
    assert clean["authorization"] == "<redacted>"


def test_an_unknown_mode_falls_back_rather_than_raising():
    set_mode("loud")
    assert current_mode() == "normal", "a logging setting must not stop a run"


def test_the_mode_comes_off_settings(monkeypatch):
    from summary_core.settings import get_settings
    monkeypatch.setenv("LQABR_SUMMARY_LOG_MODE", "debug")
    assert get_settings(refresh=True).log_mode == "debug"
    monkeypatch.setenv("LQABR_SUMMARY_LOG_MODE", "loud")
    assert get_settings(refresh=True).log_mode == "normal"


def test_configure_logging_sets_the_mode(tmp_path):
    import logging
    from summary_core.summary_logging import STREAMS, configure_logging
    root = logging.getLogger("lqabr.summary")
    root.handlers.clear()
    for stream in STREAMS:
        root.getChild(stream).handlers.clear()
    try:
        configure_logging("INFO", str(tmp_path), mode="debug")
        assert current_mode() == "debug"
    finally:
        root.handlers.clear()
        for stream in STREAMS:
            root.getChild(stream).handlers.clear()


def test_debug_spills_an_audit_hop_instead_of_one_giant_line():
    """The process branch already spilled in debug; the hop branch did not, so
    a write hop printed the whole 1,600-character note as ONE console line —
    complete, and unreadable. Same treatment, same width invariant."""
    set_mode("debug")
    logger = logging.getLogger("lqabr.test.hopspill")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Sink(165)
    logger.addHandler(sink)

    body = "X" * 4000
    SummaryLogging(run_id="res-hop", logger=logger).hop(
        service="mcp", endpoint="http://localhost:8080/mcp", status=200,
        duration_ms=1.0, attempt=2,
        params={"tool": "upsert", "authorization": "Bearer NEVER", "note": body})

    lines = [line for block in sink.rendered for line in block.split("\n")]
    assert max(len(line) for line in lines) <= 165, "the width invariant holds"
    assert sum(line.count("X") for line in lines) == 4000, "and nothing was dropped"
    assert any("attempt 2" in line for line in lines), "the retry is on the line"
    assert any("authorization: <redacted>" in line for line in lines)
    assert not any("Bearer NEVER" in line for line in lines)
