"""The two background handoffs must survive anything Steps 3-8 throw.

Both routes answer 200 immediately and hand off in-process via FastAPI
BackgroundTasks. Nothing awaits the result, so these handlers are the only
place an unhandled failure is ever recorded — if one raises, the exception
is swallowed by the task runner and the lead disappears with no trace.

_handoff_new_lead shipped as `except Exception:` while its body formatted
`exc`. So on any failure the handler itself raised NameError: the original
exception was destroyed and the log line was never written — precisely the
diagnostic it exists to produce. Its sibling _handoff_call_report had
`as exc` and was fine, which is why the asymmetry survived review.

pyflakes catches this exact fault (`undefined name 'exc'`); no test did.
"""

from __future__ import annotations

import logging
import sys

import pytest


class _Boom(RuntimeError):
    pass


@pytest.fixture
def recorded(tv_tools, monkeypatch):
    """Capture obs.log_process instead of asserting on captured stdout —
    observability.configure() installs its handlers once per process and
    cannot be re-armed mid-suite."""
    calls = []
    monkeypatch.setattr(tv_tools.obs, "log_process",
                        lambda *a, **kw: calls.append((a, kw)))
    return calls


def _stub_text_voice(monkeypatch, **attrs):
    """The handoffs import text_voice INSIDE the function, so the stub has to
    be in sys.modules at call time, not at collection time."""
    module = sys.modules.get("text_voice")
    if module is None:
        module = type(sys)("text_voice")
        monkeypatch.setitem(sys.modules, "text_voice", module)
    for name, value in attrs.items():
        monkeypatch.setattr(module, name, value, raising=False)
    return module


def test_new_lead_handoff_records_the_exception_instead_of_raising(
        tv_tools, recorded, monkeypatch):
    def explode(object_id):
        raise _Boom("hubspot fell over")

    _stub_text_voice(monkeypatch, handle_new_lead=explode)

    # Must not raise: BackgroundTasks would swallow it and the lead would
    # vanish with nothing written anywhere.
    tv_tools._handoff_new_lead("904", "corr-1")

    assert recorded, "an unhandled handoff failure must be logged"
    _, kwargs = recorded[-1]
    assert kwargs["level"] == logging.ERROR
    assert kwargs["object_id"] == "904"
    assert "_Boom" in kwargs["error"], "the exception TYPE must survive"
    assert "hubspot fell over" in kwargs["error"], "and its message"


def test_call_report_handoff_records_the_exception_instead_of_raising(
        tv_tools, recorded, monkeypatch):
    def explode(message):
        raise _Boom("classifier fell over")

    _stub_text_voice(monkeypatch, handle_call_report=explode)

    tv_tools._handoff_call_report({"call": {"id": "call-1"}}, "corr-2")

    assert recorded
    _, kwargs = recorded[-1]
    assert kwargs["level"] == logging.ERROR
    assert kwargs["call_id"] == "call-1"
    assert "_Boom" in kwargs["error"]
    assert "classifier fell over" in kwargs["error"]


def test_new_lead_handoff_truncates_a_huge_message(tv_tools, recorded, monkeypatch):
    def explode(object_id):
        raise _Boom("x" * 5000)

    _stub_text_voice(monkeypatch, handle_new_lead=explode)
    tv_tools._handoff_new_lead("904", "corr-3")

    _, kwargs = recorded[-1]
    assert len(kwargs["error"]) <= 300


def test_a_stopped_result_logs_at_warning_not_error(tv_tools, recorded, monkeypatch):
    """"stopped" is normal flow — dedup, not-qualified. Only a real error is
    ERROR, or the signal drowns."""
    _stub_text_voice(monkeypatch,
                     handle_new_lead=lambda object_id: {
                         "status": "stopped", "reason": "in-flight: ..."})
    tv_tools._handoff_new_lead("904", "corr-4")

    _, kwargs = recorded[-1]
    assert kwargs["level"] == logging.WARNING


def test_an_initiated_result_logs_nothing(tv_tools, recorded, monkeypatch):
    _stub_text_voice(monkeypatch,
                     handle_new_lead=lambda object_id: {"status": "initiated"})
    tv_tools._handoff_new_lead("904", "corr-5")
    assert not recorded, "the happy path has its own step records already"
