"""Unit tests for agents/text_voice/src/adk_agent.py — root_agent.

root_agent is a custom google.adk.agents.BaseAgent — plain-code dispatch,
no model, no template/graph workflow (see the module docstring in
adk_agent.py for why). It only matters for manual `adk web`/`adk run` use;
the live webhook path (tools.py) never touches it — that's exactly why this
is a separate module and a separate test file from test_text_voice.py.

These tests exercise the dispatch logic directly against monkeypatched
Steps 3/7/8 functions (get_lead, handle_new_lead, handle_call_report,
list_text_voice_queue — patched on the `tv_agent` fixture, since that's the
actual module object `adk_agent.py`'s `text_voice.<fn>()` calls resolve
against; see conftest.py's `tv_adk_agent` fixture for why the two fixtures
must share that one module instance). No real ADK runtime, no real
InvocationContext — just a fake object with the one attribute
(`.user_content`) the dispatch code actually reads.
"""

import asyncio
import json
from types import SimpleNamespace


def _fake_ctx(text):
    """A stand-in for InvocationContext exposing only what the agent reads."""
    if text is None:
        return SimpleNamespace(user_content=None)
    return SimpleNamespace(
        user_content=SimpleNamespace(parts=[SimpleNamespace(text=text)]))


def _run(agent, ctx):
    """Drain the async generator `_run_async_impl` yields, without needing
    pytest-asyncio (not a dependency of this suite)."""
    async def _collect():
        return [event async for event in agent._run_async_impl(ctx)]
    return asyncio.run(_collect())


def _reply_text(event):
    return event.content.parts[0].text


def test_textvoiceagent_bare_id_does_a_readonly_lookup_not_a_dial(
        tv_agent, tv_adk_agent, monkeypatch):
    calls = []
    monkeypatch.setattr(tv_agent, "get_lead", lambda oid: calls.append(("get_lead", oid)) or {"object_id": oid})
    monkeypatch.setattr(tv_agent, "handle_new_lead", lambda oid: calls.append(("handle_new_lead", oid)))

    events = _run(tv_adk_agent.root_agent, _fake_ctx("904"))

    assert calls == [("get_lead", "904")]  # never dials
    assert json.loads(_reply_text(events[0])) == {"object_id": "904"}


def test_textvoiceagent_run_prefix_places_a_real_call(tv_agent, tv_adk_agent, monkeypatch):
    calls = []
    monkeypatch.setattr(tv_agent, "handle_new_lead", lambda oid: calls.append(oid) or {"object_id": oid})

    events = _run(tv_adk_agent.root_agent, _fake_ctx("run 904"))

    assert calls == ["904"]
    assert json.loads(_reply_text(events[0])) == {"object_id": "904"}


def test_textvoiceagent_test_prefix_stops_cleanly_when_dial_fails(
        tv_agent, tv_adk_agent, monkeypatch):
    """`test <id>` must not reach the Vapi polling stage when the dial
    produced no call_id — the flow reports and stops instead of polling."""
    monkeypatch.setattr(tv_agent, "handle_new_lead",
                        lambda oid: {"status": "stopped", "reason": "not-found"})
    polled = []
    monkeypatch.setattr(tv_adk_agent.tv_tools, "_vapi",
                        lambda: polled.append(1))

    events = _run(tv_adk_agent.root_agent, _fake_ctx("test 904"))

    texts = [_reply_text(e) for e in events]
    assert any("handle_new_lead" in t for t in texts)      # stage 1 announced
    assert any("No call_id" in t for t in texts)           # clean stop message
    assert polled == []                                    # never polled Vapi


def test_textvoiceagent_json_input_dispatches_to_call_report(tv_agent, tv_adk_agent, monkeypatch):
    calls = []
    monkeypatch.setattr(tv_agent, "handle_call_report",
                        lambda report: calls.append(report) or {"status": "ok"})

    events = _run(tv_adk_agent.root_agent, _fake_ctx('{"endedReason": "customer-ended-call"}'))

    assert calls == [{"endedReason": "customer-ended-call"}]
    assert json.loads(_reply_text(events[0])) == {"status": "ok"}


def test_textvoiceagent_malformed_json_reports_the_error_not_a_crash(tv_agent, tv_adk_agent):
    events = _run(tv_adk_agent.root_agent, _fake_ctx("{not json"))
    assert "Not valid JSON" in _reply_text(events[0])


def test_textvoiceagent_queue_keyword_lists_the_queue(tv_agent, tv_adk_agent, monkeypatch):
    monkeypatch.setattr(tv_agent, "list_text_voice_queue",
                        lambda: {"count": 0, "leads": []})

    events = _run(tv_adk_agent.root_agent, _fake_ctx("queue"))

    assert json.loads(_reply_text(events[0])) == {"count": 0, "leads": []}


def test_textvoiceagent_empty_input_shows_help_not_an_error(tv_agent, tv_adk_agent):
    events = _run(tv_adk_agent.root_agent, _fake_ctx(None))
    assert "Type a contact id" in _reply_text(events[0])
