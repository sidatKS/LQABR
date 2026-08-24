"""Unit tests for the Vapi fixes (2026-08-04): route -> /call-report (renamed
2026-08-05 to /vapi_report, then 2026-08-06 to /voice_agent/vapi_report),
B3 retry (backoffPlan), wasted-backoff, M5 idempotency, and the Anthropic env
bridge.
All providers mocked — no calls, no network."""
import types as _pytypes

import pytest

import tools
import text_voice
from lqabr_core.types import VoiceLead


def _lead(**kw):
    base = dict(phone_number="+15555550123", object_id="C1",
                employee_id="E1", full_name="Ada Lovelace",
                company_name="Analytical Engines", industry="software",
                job_title="Engineer")
    base.update(kw)
    return VoiceLead(**base)


# ------------------------------------------------------------- server override
# (The route-fix / backoffPlan / localhost-guard tests that used to live here
# were deleted 2026-08-19 along with the per-call `server` override itself:
# the report destination, secret and retries now live only on the dashboard
# assistant's server config. test_tools.py's
# test_build_call_payload_dashboard_assistant_sends_id_and_no_server_override
# guards the new invariant — no per-call override, ever.)


# ------------------------------------ dashboard assistant is now mandatory
def test_unset_assistant_id_raises_instead_of_building_a_partial_payload(monkeypatch):
    """2026-08-07: the transient assistant (build_assistant_config and the
    ASSISTANT_* prompt constants) was removed — it was dead code, since
    LQABR_VAPI_ASSISTANT_ID is set everywhere and the `assistantId` branch
    always won. With no fallback script left in the repo, an unset id must fail
    loudly rather than POST a call body with no assistant at all."""
    monkeypatch.setattr(tools, "VAPI_ASSISTANT_ID", "")
    with pytest.raises(tools.VapiError, match="LQABR_VAPI_ASSISTANT_ID is unset"):
        tools.build_call_payload(_lead())


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
    def record_call_outcome(self, cid, outcome, detail=None, current=None):
        self.recorded.append((cid, outcome))
        return {"status": "ok", "events": [], "failures": [],
                "probability": 60, "stage": "x", "promoted_to_scheduling": True}


_REPORT = {
    "type": "end-of-call-report",
    "endedReason": "customer-did-not-answer",  # terminal -> no model call
    "artifact": {"transcript": "", "recordingUrl": ""},
    "call": {"id": "call_1"},
    "assistantOverrides": {"variableValues": {"object_id": "C1"}},
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
    """LQABR_ANTHROPIC_API_KEY (how Cloud Run --set-secrets delivers the
    Secret Manager value) resolves via get_secret's `auto` source."""
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


def test_anthropic_key_overwrites_a_preset_env_var(monkeypatch):
    """2026-08-20 (user decision): Secret Manager DIRECTLY, no more
    three-name confusion. A pre-set ANTHROPIC_API_KEY shell variable used to
    silently win before Secret Manager was ever consulted (the old
    ensure_provider_key behaviour); now it is overwritten every process, so
    the single source lqabr-anthropic-api-key is always what litellm sees."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stale-shell-value")
    monkeypatch.delenv("LQABR_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(text_voice, "get_secret",
                        lambda name: "sk-from-secret-manager")

    captured = {}
    monkeypatch.setitem(__import__("sys").modules, "litellm", _fake_litellm(captured))

    text_voice._model_classify("assistant-ended-call", "hi there yes yes")
    assert captured.get("ran")
    assert __import__("os").environ["ANTHROPIC_API_KEY"] == "sk-from-secret-manager"
