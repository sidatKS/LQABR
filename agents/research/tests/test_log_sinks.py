"""Phase 1 — the sink split. One file per stream, and nothing it can kill.

The acceptance list from §6 of claude/LOG_DESIGN_2026-08-26.md. The behaviour
each test pins was first proved by running it; these exist so it stays proved.
"""

from __future__ import annotations

import json
import logging

import pytest

from research_core.research_logging import (ResearchLogging, STREAMS, configure_logging,
                               sink_state)

FILES = ("research_process.log", "research_audit.log", "research_system.log")


def _fresh() -> logging.Logger:
    root = logging.getLogger("lqabr.research")
    root.handlers.clear()
    for stream in STREAMS:
        root.getChild(stream).handlers.clear()
    return root


@pytest.fixture(autouse=True)
def _clean_handlers():
    _fresh()
    yield
    _fresh()


def _one_of_each(run_id: str = "res-sink") -> ResearchLogging:
    run_log = ResearchLogging(run_id=run_id)
    run_log.process.emit("run_start", objectId="1")
    run_log.outbound_call(service="mcp", endpoint="http://localhost:8080/mcp", status=200,
            duration_ms=7.0)
    run_log.system.emit("service_start", service="lqabr-research-agent")
    return run_log


class _Recorder(logging.Handler):
    """Attached to the PARENT before configure_logging.

    Two jobs: it proves child records propagate up (which is what keeps the
    console a single narrative), and it catches the sink's own warnings —
    `log_sink_unavailable`, `log_export_disabled` — which are
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
    configure_logging("INFO", str(tmp_path), "json")
    _one_of_each()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(FILES)


def test_a_process_record_lands_in_process_and_nowhere_else(tmp_path):
    configure_logging("INFO", str(tmp_path), "json")
    _one_of_each()
    assert [r["event"] for r in _records(tmp_path / "research_process.log")] == ["run_start"]
    for other in ("research_audit.log", "research_system.log"):
        assert "run_start" not in [r["event"] for r in _records(tmp_path / other)]


def test_an_audit_hop_lands_in_audit_only(tmp_path):
    configure_logging("INFO", str(tmp_path), "json")
    _one_of_each()
    audit = _records(tmp_path / "research_audit.log")
    assert [r["stream"] for r in audit] == ["audit"]
    assert audit[0]["event"] == "outbound_call"
    for other in ("research_process.log", "research_system.log"):
        assert all(r["stream"] != "audit" for r in _records(tmp_path / other))


def test_the_parent_still_receives_all_three(tmp_path):
    """Propagation intact: three files on disk, one story on screen. The
    console handler lives on the PARENT and the records are emitted on the
    CHILDREN — if this fails, the console loses the narrative."""
    recorder = _recording()
    configure_logging("INFO", str(tmp_path), "json")
    _one_of_each()
    # Only this run's records: configure_logging also emits its own sink notes
    # (log_export_disabled, log_sink_otlp) under a run id it mints itself.
    mine = [r for r in recorder.events() if r["run_id"] == "res-sink"]
    assert sorted(r["stream"] for r in mine) == ["audit", "process", "system"]


def test_an_empty_log_dir_writes_nothing_and_raises_nothing(tmp_path):
    recorder = _recording()
    configure_logging("INFO", "", "json")
    _one_of_each()
    assert list(tmp_path.iterdir()) == []
    assert sink_state()["files"] == {}
    mine = [r for r in recorder.events() if r["run_id"] == "res-sink"]
    assert len(mine) == 3, "the streams still flow, file-less"


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
        configure_logging("INFO", str(blocked), "json")
        _one_of_each()
        events = recorder.events()
        unavailable = [e for e in events if e.get("event") == "log_sink_unavailable"]
        assert len(unavailable) == 3, "one per stream, each naming its own path"
        assert all(e["reason"] for e in unavailable), "a named reason, not a bare flag"
        assert sorted(sink_state()["degraded"]) == ["audit:open", "process:open",
                                                    "system:open"]
        assert any(e.get("event") == "run_start" for e in events), \
            "the run still completes, console-only"
    finally:
        blocked.chmod(0o700)


def test_every_line_of_every_file_parses_as_json(tmp_path):
    configure_logging("INFO", str(tmp_path), "json")
    _one_of_each()
    for name in FILES:
        for line in (tmp_path / name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)


# The two rotation tests that lived here are gone with the behaviour they
# covered: the per-stream files are plain and uncapped — one fixed name each,
# no size cap, no backups, no day sweep. `test_file_grows_uncapped_no_rotation`
# in test_log_export.py is what asserts that now.
