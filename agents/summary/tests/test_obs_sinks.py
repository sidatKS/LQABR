"""Phase 1 — the sink split, summary side. One file per stream.

The acceptance list from §6 of claude/LOG_DESIGN_2026-08-26.md. The behaviour
each test pins was first proved by running it; these exist so it stays proved.
"""

from __future__ import annotations

import json
import logging

import pytest

from summary_core.summary_logging import (SummaryLogging, STREAMS, configure_logging,
                               sink_state)

FILES = ("summary_process.log", "summary_audit.log", "summary_system.log")


def _fresh() -> logging.Logger:
    root = logging.getLogger("lqabr.summary")
    root.handlers.clear()
    for stream in STREAMS:
        root.getChild(stream).handlers.clear()
    return root


@pytest.fixture(autouse=True)
def _clean_handlers():
    _fresh()
    yield
    _fresh()


def _one_of_each(run_id: str = "sum-sink") -> SummaryLogging:
    obs = SummaryLogging(run_id=run_id)
    obs.process.emit("agent_build", model="gemini")
    obs.hop(service="http", endpoint="https://spring.io/blog", method="GET",
            status=200, duration_ms=7.0)
    obs.system.emit("service_start", service="lqabr-summary-agent")
    return obs


class _Recorder(logging.Handler):
    """Attached to the PARENT before configure_logging.

    Two jobs: it proves child records propagate up (which is what keeps the
    console a single narrative), and it catches the sink's own warnings —
    `log_sink_legacy`, `log_sink_unavailable`, `log_rotate_failed` — which are
    emitted during configure_logging itself, before any test could look at
    stdout. Because the parent then already has a handler, configure_logging
    adds no console handler and stdout stays clean.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lines: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def events(self) -> list:
        out = []
        for line in self.lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out


def _recording() -> _Recorder:
    root = _fresh()
    recorder = _Recorder()
    root.addHandler(recorder)
    return recorder


def _records(path) -> list:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_three_files_with_exactly_the_six_names(tmp_path):
    configure_logging("INFO", str(tmp_path))
    _one_of_each()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(FILES)


def test_a_process_record_lands_in_process_and_nowhere_else(tmp_path):
    configure_logging("INFO", str(tmp_path))
    _one_of_each()
    assert [r["event"] for r in _records(tmp_path / "summary_process.log")] == ["agent_build"]
    for other in ("summary_audit.log", "summary_system.log"):
        assert "agent_build" not in [r["event"] for r in _records(tmp_path / other)]


def test_an_audit_hop_lands_in_audit_only(tmp_path):
    configure_logging("INFO", str(tmp_path))
    _one_of_each()
    audit = _records(tmp_path / "summary_audit.log")
    assert [r["stream"] for r in audit] == ["audit"]
    assert audit[0]["event"] == "outbound_call"
    for other in ("summary_process.log", "summary_system.log"):
        assert all(r["stream"] != "audit" for r in _records(tmp_path / other))


def test_the_parent_still_receives_all_three(tmp_path):
    """Propagation intact: three files on disk, one story on screen. The
    console handler lives on the PARENT and the records are emitted on the
    CHILDREN — if this fails, the console loses the narrative."""
    recorder = _recording()
    configure_logging("INFO", str(tmp_path))
    _one_of_each()
    assert sorted(r["stream"] for r in recorder.events()) == ["audit", "process",
                                                              "system"]


def test_an_empty_log_dir_writes_nothing_and_raises_nothing(tmp_path):
    recorder = _recording()
    configure_logging("INFO", "")
    _one_of_each()
    assert list(tmp_path.iterdir()) == []
    assert sink_state()["files"] == {}
    assert len(recorder.events()) == 3, "the streams still flow, file-less"


def _really_unwritable(path) -> bool:
    """Does chmod actually deny us this directory?

    Asked empirically, not by sniffing `sys.platform`: on the Windows
    filesystem these files live on, and for root anywhere, `chmod(0o500)` is
    advisory and the write still succeeds — so the test that follows would
    assert a degradation that never happened.
    """
    probe = path / ".probe"
    try:
        probe.write_text("x")
    except OSError:
        return True
    probe.unlink(missing_ok=True)
    return False


def test_an_unwritable_dir_degrades_and_names_itself(tmp_path):
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    if not _really_unwritable(blocked):
        blocked.chmod(0o700)
        pytest.skip("chmod is advisory here (Windows filesystem, or root) "
                    "— this test would assert a degradation that never happened")
    try:
        recorder = _recording()
        configure_logging("INFO", str(blocked))
        _one_of_each()
        events = recorder.events()
        unavailable = [e for e in events if e.get("event") == "log_sink_unavailable"]
        assert len(unavailable) == 3, "one per stream, each naming its own path"
        assert all(e["reason"] for e in unavailable), "a named reason, not a bare flag"
        assert sorted(sink_state()["degraded"]) == ["audit:open", "process:open",
                                                    "system:open"]
        assert any(e.get("event") == "agent_build" for e in events), \
            "the run still completes, console-only"
    finally:
        blocked.chmod(0o700)


def test_the_deprecated_log_file_still_works_and_announces_itself(tmp_path):
    legacy = tmp_path / "agent.log"
    recorder = _recording()
    configure_logging("INFO", "", log_file=str(legacy))
    _one_of_each()
    announcement = [e for e in recorder.events() if e.get("event") == "log_sink_legacy"]
    assert announcement and announcement[0]["path"] == str(legacy)
    assert sorted({r["stream"] for r in _records(legacy)}) == ["audit", "process", "system"]


def test_every_line_of_every_file_parses_as_json(tmp_path):
    configure_logging("INFO", str(tmp_path))
    _one_of_each()
    for name in FILES:
        for line in (tmp_path / name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)


def test_a_small_ceiling_rolls_over_and_the_live_file_keeps_its_name(tmp_path):
    configure_logging("INFO", str(tmp_path), max_bytes=400, backups=2)
    obs = SummaryLogging(run_id="sum-roll")
    for n in range(12):
        obs.process.emit("agent_build", n=str(n), filler="x" * 60)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "summary_process.log" in names, "the live file keeps the exact name"
    assert "summary_process.log.1" in names


def test_a_rollover_that_raises_is_reported_once_and_writing_continues(tmp_path,
                                                                      monkeypatch):
    """WinError 32: another handle holds the file. Uncaught, this spams stderr
    on every subsequent emit. SummaryLogging must never kill a run."""
    import summary_core.summary_logging as obs_module

    def _boom(self):
        raise PermissionError(32, "used by another process")

    monkeypatch.setattr(obs_module.RotatingFileHandler, "doRollover", _boom)
    recorder = _recording()
    configure_logging("INFO", str(tmp_path), max_bytes=300, backups=1)
    obs = SummaryLogging(run_id="sum-boom")
    for n in range(10):
        obs.process.emit("agent_build", n=str(n), filler="y" * 60)

    failures = [e for e in recorder.events() if e.get("event") == "log_rotate_failed"]
    assert len(failures) == 1, "reported once, not on every subsequent emit"
    assert failures[0]["sink"] == "process" and failures[0]["reason"]
    written = _records(tmp_path / "summary_process.log")
    assert len(written) == 10, "every record still reached the file"
    assert sink_state()["degraded"] == ["process:rotate"]
