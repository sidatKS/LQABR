"""Unit tests for the Vapi fixes (2026-08-04): route -> /call-report (renamed
2026-08-05 to /vapi_report, then 2026-08-06 to /voice_agent/vapi_report),
B3 retry (backoffPlan), wasted-backoff, M5 idempotency, and the Anthropic env
bridge.
All providers mocked — no calls, no network."""
import json
import os
import types as _pytypes

import pytest

import tools
import text_voice
from lqabr_core.types import VoiceLead


def _lead(**kw):
    base = dict(phone_number="+15555550123", contact_id="C1",
                employee_id="E1", full_name="Ada Lovelace",
                company_name="Analytical Engines", industry="software",
                job_title="Engineer")
    base.update(kw)
    return VoiceLead(**base)


# ---------------------------------------------------------------- route fix
# The server URL must equal the configured report callback on BOTH call paths.
# Asserting equality (not a hard-coded path) keeps these robust when a local
# .env sets an explicit LQABR_VAPI_REPORT_CALLBACK_URL override.
def test_transient_server_url_matches_report_callback(monkeypatch):
    monkeypatch.setattr(tools, "VAPI_ASSISTANT_ID", "")
    payload = tools.build_call_payload(_lead())
    assert payload["assistant"]["server"]["url"] == tools.VAPI_REPORT_CALLBACK_URL


def test_assistantid_branch_server_url_matches_report_callback(monkeypatch):
    monkeypatch.setattr(tools, "VAPI_ASSISTANT_ID", "asst_123")
    payload = tools.build_call_payload(_lead())
    assert payload["assistantOverrides"]["server"]["url"] == tools.VAPI_REPORT_CALLBACK_URL


def test_report_callback_default_path_is_voice_agent_vapi_report():
    """The route change: with no explicit override, the default report path is
    /voice_agent/vapi_report (was /vapi_report, before that /call-report,
    before that /vapi/report). Skipped when an override is set (e.g. a local
    .env exports LQABR_VAPI_REPORT_CALLBACK_URL) since that bypasses the
    default entirely."""
    if os.environ.get("LQABR_VAPI_REPORT_CALLBACK_URL"):
        pytest.skip("LQABR_VAPI_REPORT_CALLBACK_URL override set (e.g. from .env)")
    assert tools.VAPI_REPORT_CALLBACK_URL.endswith("/voice_agent/vapi_report")
    assert "/call-report" not in tools.VAPI_REPORT_CALLBACK_URL
    assert "/vapi/report" not in tools.VAPI_REPORT_CALLBACK_URL


# ------------------------------------------------------------- B3 backoffPlan
def test_transient_server_has_retries():
    server = tools.build_assistant_config()["server"]
    assert server["backoffPlan"]["maxRetries"] == 2
    # Vapi 400s the entire POST /call if backoffPlan omits this (live 2026-08-04).
    assert 0 <= server["backoffPlan"]["baseDelaySeconds"] <= 10


def test_assistantid_branch_server_has_retries(monkeypatch):
    monkeypatch.setattr(tools, "VAPI_ASSISTANT_ID", "asst_123")
    payload = tools.build_call_payload(_lead())
    assert payload["assistantOverrides"]["server"]["backoffPlan"]["maxRetries"] == 2
    assert 0 <= payload["assistantOverrides"]["server"]["backoffPlan"]["baseDelaySeconds"] <= 10


# ------------------------------------------------------ B1 (already fixed)
def test_endcall_tool_present_and_no_legacy_flag():
    cfg = tools.build_assistant_config()
    assert {"type": "endCall"} in cfg["model"]["tools"]
    assert "endCallFunctionEnabled" not in json.dumps(cfg)


def test_server_messages_only_end_of_call_report():
    assert tools.build_assistant_config()["serverMessages"] == ["end-of-call-report"]


# ------------------------------------------------------- wasted backoff
class _Resp:
    def __init__(self, status): self.status_code, self.text = status, "err"
    def json(self): return {}


def test_no_sleep_after_final_attempt(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tools.time, "sleep", lambda s: sleeps.append(s))

    class _Sess:
        def request(self, *a, **k): return _Resp(500)  # always retryable

    client = tools.VapiClient(api_key="k", session=_Sess(),
                              max_retries=3, backoff_seconds=0.01)
    with pytest.raises(tools.VapiError):
        client._request("POST", "/call", json={})
    # 3 attempts -> only 2 sleeps (none after the last attempt)
    assert len(sleeps) == 2, sleeps


# --------------------------------------------------------- M5 idempotency
class _FakeMcp:
    def __init__(self, lead): self._lead, self.recorded = lead, []
    def get_lead(self, cid): return self._lead
    def record_call_outcome(self, cid, outcome, detail=None):
        self.recorded.append((cid, outcome))
        return {"status": "ok", "events": [], "failures": [],
                "probability": 60, "stage": "x", "promoted_to_scheduling": True}


_REPORT = {
    "type": "end-of-call-report",
    "endedReason": "customer-did-not-answer",  # terminal -> no model call
    "artifact": {"transcript": "", "recordingUrl": ""},
    "call": {"id": "call_1"},
    "assistantOverrides": {"variableValues": {"contact_id": "C1"}},
}


def test_duplicate_report_skipped_when_terminal(monkeypatch):
    fake = _FakeMcp(_lead(voice_status="COMPLETED"))  # is_complete == True
    monkeypatch.setattr(text_voice, "mcp", fake)
    out = text_voice.handle_call_report(dict(_REPORT))
    assert out["status"] == "duplicate", out
    assert fake.recorded == [], "Step 8 must not run on a duplicate"


def test_fresh_report_processed_when_in_flight(monkeypatch):
    fake = _FakeMcp(_lead(voice_status="INITIATED"))  # is_complete == False
    monkeypatch.setattr(text_voice, "mcp", fake)
    out = text_voice.handle_call_report(dict(_REPORT))
    assert out["status"] != "duplicate", out
    assert fake.recorded and fake.recorded[0][0] == "C1"


# ---------------------------------------- Anthropic key bridge / Secret Manager
def _fake_litellm(captured):
    class _Msg:  # minimal litellm response shape
        content = '{"outcome": "answered_and_engaged", "summary": "ok"}'

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 1
        completion_tokens = 1

    class _Resp2:
        choices = [_Choice()]
        usage = _Usage()

    fake_litellm = _pytypes.ModuleType("litellm")
    def _completion(**kw):
        captured["ran"] = True
        return _Resp2()
    fake_litellm.completion = _completion
    return fake_litellm


def test_anthropic_key_bridged_from_lqabr_name(monkeypatch):
    """LQABR_ANTHROPIC_API_KEY (e.g. Cloud Run --set-secrets) still wins
    without ever touching Secret Manager."""
    from lqabr_core.secrets import get_secret
    get_secret.cache_clear()  # avoid lru_cache bleed from other tests

    monkeypatch.setenv("LQABR_ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    captured = {}
    monkeypatch.setitem(__import__("sys").modules, "litellm", _fake_litellm(captured))

    text_voice._model_classify("assistant-ended-call", "hi there yes yes")
    assert captured.get("ran")
    assert __import__("os").environ["ANTHROPIC_API_KEY"] == "sk-test-123"
    get_secret.cache_clear()


def test_anthropic_key_falls_back_to_secret_manager(monkeypatch):
    """2026-08-07: with no ANTHROPIC_API_KEY and no LQABR_ANTHROPIC_API_KEY
    env var set, Step 7's classification path now reaches Secret Manager via
    lqabr_core.model.ensure_provider_key() — this used to be env-only and
    silently skip the model call instead."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LQABR_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("lqabr_core.model.get_secret",
                         lambda name: "sk-from-secret-manager")

    captured = {}
    monkeypatch.setitem(__import__("sys").modules, "litellm", _fake_litellm(captured))

    text_voice._model_classify("assistant-ended-call", "hi there yes yes")
    assert captured.get("ran")
    assert __import__("os").environ["ANTHROPIC_API_KEY"] == "sk-from-secret-manager"
