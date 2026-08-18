"""Unit tests for agents/text_voice/src/text_voice.py — Steps 3, 7, 8 and both
inbound entrypoints (handle_new_lead, handle_call_report).

Strategy: `mcp` (the module-level Step 5 stand-in) and `place_call` (imported
from tools.py into this module's namespace) are monkeypatched with fakes at
the point they are referenced inside text_voice.py's own globals. This tests
Steps 3/7/8's own logic — dedup/claim ordering, outcome classification,
partial-write reporting — without needing a real HubSpot/Vapi credential or
network call, and without going through `_MCPAdapter`'s HubSpot-specific
mapping (that class gets its own tests below, against a fake `crm._request`).
"""

import time

from lqabr_core.crm.base import CRMError
from lqabr_core.types import EventType, VoiceLead, VoiceOutcome


# ============================================================== fake mcp/vapi

class FakeMCP:
    """Records every call. Each method's return/raise is scripted per test."""

    def __init__(self):
        self.calls = []
        self.get_lead_result = None
        self.get_lead_error = None
        self.lead_context_result = ""
        # Default: the in-flight claim was written just now, so an INITIATED /
        # CALL_PLACED lead is inside the stuck window and stays blocked.
        # Tests that want a STALE claim set this to an older epoch-ms value.
        self.voice_status_written_ms_result = int(time.time() * 1000)
        self.upsert_results = []  # list of (result_or_exception)
        self.record_call_outcome_result = None
        self.record_call_outcome_error = None
        self.find_lead_by_phone_result = None
        self.find_lead_by_phone_error = None
        self.leads_in_stage_result = []
        self.leads_in_stage_error = None

    def get_lead(self, object_id):
        self.calls.append(("get_lead", object_id))
        if self.get_lead_error:
            raise self.get_lead_error
        return self.get_lead_result

    def get_lead_with_extras(self, object_id):
        """The lead plus the properties VoiceLead cannot carry."""
        self.calls.append(("get_lead_with_extras", object_id))
        if self.get_lead_error:
            raise self.get_lead_error
        return self.get_lead_result, {
            "lead_context": self.lead_context_result,
            "voice_status_written_ms": self.voice_status_written_ms_result,
        }

    def upsert_lead(self, contact_id, voice_status=None, probability=None, outcome=None):
        self.calls.append(("upsert_lead", contact_id, voice_status, probability, outcome))
        if self.upsert_results:
            result = self.upsert_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return {"status": "updated", "contact_id": contact_id}

    def record_call_outcome(self, contact_id, outcome, detail=None):
        self.calls.append(("record_call_outcome", contact_id, outcome, detail))
        if self.record_call_outcome_error:
            raise self.record_call_outcome_error
        return self.record_call_outcome_result

    def find_lead_by_phone(self, phone):
        self.calls.append(("find_lead_by_phone", phone))
        if self.find_lead_by_phone_error:
            raise self.find_lead_by_phone_error
        return self.find_lead_by_phone_result

    def leads_in_stage(self, stage, limit=100):
        self.calls.append(("leads_in_stage", stage, limit))
        if self.leads_in_stage_error:
            raise self.leads_in_stage_error
        return self.leads_in_stage_result


def _voice_lead(**overrides):
    base = dict(employee_id="E1", contact_id="123", phone_number="+15550001111",
                full_name="Jane Smith", opted_out=False, voice_status="PENDING",
                probability=30, email_status="OPENED")
    base.update(overrides)
    return VoiceLead(**base)


# ==================================================================== Step 3

def test_get_lead_stops_when_contact_not_found(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = None
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("904")
    assert result["callable"] is False
    assert result["reason"].startswith("not-found")
    # Keyed "object_id", not "contact_id": no contact was ever confirmed to
    # exist for this id, so it's still just the raw value the gateway sent.
    assert result["object_id"] == "904"


def test_get_lead_reports_crm_error_distinctly_from_not_found(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_error = CRMError("HubSpot 500")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("904")
    assert result["callable"] is False
    assert result["reason"].startswith("crm-error:")
    assert "not-found" not in result["reason"]
    assert result["object_id"] == "904"


def test_get_lead_blocks_opted_out_contact(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(opted_out=True)
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is False
    assert result["reason"].startswith("opted-out")


def test_get_lead_blocks_already_complete_contact(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="COMPLETED")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is False
    assert result["reason"] == "already-complete: voice_status=COMPLETED"


def test_get_lead_blocks_in_flight_contact(tv_agent, monkeypatch):
    """The dedup guard this session's fix added: INITIATED blocks a redelivery
    from dialing the same lead twice."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="INITIATED")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is False
    assert result["reason"].startswith("in-flight: voice_status=INITIATED")


def test_get_lead_blocks_lead_that_never_met_the_email_trigger(tv_agent, monkeypatch):
    """Defense-in-depth on the Rev 5 trigger: the workflow is the primary
    gate, but /voice_agent/lead has no auth and the workflow isn't configured yet — a
    contact whose email_status never reached OPENED must be refused here
    too, not dialed."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(email_status=None)
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is False
    assert result["reason"].startswith("not-qualified: email_status=unset")


def test_get_lead_blocks_email_status_below_opened(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(email_status="SENT")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is False
    assert result["reason"].startswith("not-qualified: email_status=SENT")


def test_get_lead_callable_when_pending_with_phone(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="PENDING")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is True
    assert result["contact_id"] == "123"


# ================================================================ Step 7 pure

def test_classify_from_ended_reason_terminal_values(tv_agent):
    assert tv_agent._classify_from_ended_reason("customer-did-not-answer") is VoiceOutcome.NOT_ANSWERED
    assert tv_agent._classify_from_ended_reason("voicemail") is VoiceOutcome.VOICEMAIL
    assert tv_agent._classify_from_ended_reason("customer-busy") is VoiceOutcome.NOT_ANSWERED
    # Real Vapi endedReason value (docs.vapi.ai/calls/call-ended-reason) — an
    # exact entry, not a prefix, since nothing else in the enum shares a
    # "pipeline-error-" prefix with it.
    assert tv_agent._classify_from_ended_reason("pipeline-no-available-llm-model") is VoiceOutcome.NOT_ANSWERED


def test_classify_from_ended_reason_failure_prefixes_count_as_not_answered(tv_agent):
    """Every value here is a real Vapi endedReason (verified against Vapi's
    documented enum), not a placeholder — the old test used fake strings like
    "pipeline-error-timeout" that happened to match dead prefixes; none of
    those prefixes matched anything Vapi actually sends."""
    assert tv_agent._classify_from_ended_reason(
        "call.in-progress.error-vapifault-worker-died") is VoiceOutcome.NOT_ANSWERED
    assert tv_agent._classify_from_ended_reason("assistant-not-found") is VoiceOutcome.NOT_ANSWERED
    assert tv_agent._classify_from_ended_reason(
        "call-start-error-neither-assistant-nor-server-set") is VoiceOutcome.NOT_ANSWERED
    assert tv_agent._classify_from_ended_reason(
        "call.start.error-subscription-frozen") is VoiceOutcome.NOT_ANSWERED


def test_classify_from_ended_reason_assistant_request_prefix_widened_to_catch_siblings(tv_agent):
    """Regression test for the too-narrow literal this replaced: Vapi documents
    five assistant-request-returned-* siblings for the same failure mode
    (assistant config never resolved) that "assistant-request-failed" alone
    used to miss entirely."""
    assert tv_agent._classify_from_ended_reason("assistant-request-failed") is VoiceOutcome.NOT_ANSWERED
    assert tv_agent._classify_from_ended_reason("assistant-request-returned-error") is VoiceOutcome.NOT_ANSWERED
    assert tv_agent._classify_from_ended_reason(
        "assistant-request-returned-no-assistant") is VoiceOutcome.NOT_ANSWERED


def test_classify_from_ended_reason_dead_prefixes_stay_removed(tv_agent):
    """These strings used to false-match the three dead prefixes this fix
    removed. None of them are real Vapi values, so None (defer to the
    model/transcript path) is the correct answer, not NOT_ANSWERED."""
    assert tv_agent._classify_from_ended_reason("pipeline-error-timeout") is None
    assert tv_agent._classify_from_ended_reason("error-vapifault-something") is None
    assert tv_agent._classify_from_ended_reason("error-providerfault-something") is None


def test_classify_from_ended_reason_unknown_or_conversational_returns_none(tv_agent):
    """A conversational ending (e.g. `customer-ended-call`) must fall through
    to the model/transcript path, not be settled here."""
    assert tv_agent._classify_from_ended_reason("customer-ended-call") is None
    assert tv_agent._classify_from_ended_reason("") is None
    assert tv_agent._classify_from_ended_reason(None) is None


def test_parse_model_reply_accepts_plain_json(tv_agent):
    outcome, summary = tv_agent._parse_model_reply(
        '{"outcome": "answered_and_engaged", "summary": "They agreed to a demo."}')
    assert outcome == "answered_and_engaged"
    assert summary == "They agreed to a demo."


def test_parse_model_reply_strips_fenced_code_block(tv_agent):
    reply = '```json\n{"outcome": "voicemail", "summary": "Left a message."}\n```'
    outcome, summary = tv_agent._parse_model_reply(reply)
    assert outcome == "voicemail"
    assert summary == "Left a message."


def test_parse_model_reply_rejects_unknown_outcome(tv_agent):
    import pytest
    with pytest.raises(ValueError, match="unknown outcome"):
        tv_agent._parse_model_reply('{"outcome": "maybe", "summary": "unclear"}')


def test_parse_model_reply_rejects_empty_summary(tv_agent):
    import pytest
    with pytest.raises(ValueError, match="empty summary"):
        tv_agent._parse_model_reply('{"outcome": "voicemail", "summary": ""}')


def test_parse_model_reply_rejects_reply_with_no_json_object(tv_agent):
    import pytest
    with pytest.raises(ValueError, match="no JSON object"):
        tv_agent._parse_model_reply("I think it went well.")


# =========================================================== Step 7 summarise_report

def test_summarise_report_skips_model_for_terminal_ended_reason(tv_agent, monkeypatch):
    called = []
    monkeypatch.setattr(tv_agent, "_model_classify", lambda *a, **kw: called.append(1))
    result = tv_agent.summarise_report(ended_reason="customer-did-not-answer", transcript="")
    assert result["outcome"] == VoiceOutcome.NOT_ANSWERED.value
    assert result["classified_by"] == "ended_reason"
    assert not called


def test_summarise_report_treats_empty_transcript_as_voicemail_not_answered_call(tv_agent, monkeypatch):
    """The structural fix for the Twilio-era bug: a connected call with no
    speech must never be credited as CALL_ANSWERED."""
    called = []
    monkeypatch.setattr(tv_agent, "_model_classify", lambda *a, **kw: called.append(1))
    result = tv_agent.summarise_report(ended_reason="customer-ended-call", transcript="   ")
    assert result["outcome"] == VoiceOutcome.VOICEMAIL.value
    assert result["classified_by"] == "empty_transcript"
    assert not called


def test_summarise_report_calls_model_when_ended_reason_is_ambiguous_and_transcript_exists(tv_agent, monkeypatch):
    monkeypatch.setattr(tv_agent, "_model_classify",
                        lambda ended_reason, transcript: {
                            "outcome": "answered_and_engaged",
                            "summary": "Interested, wants a calendar link.",
                            "classified_by": "model"})
    result = tv_agent.summarise_report(ended_reason="customer-ended-call",
                                       transcript="Yes, that would be great.")
    assert result["outcome"] == "answered_and_engaged"
    assert result["classified_by"] == "model"
    assert result["ended_reason"] == "customer-ended-call"


def test_summarise_report_falls_back_when_model_raises(tv_agent, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("litellm timeout")

    monkeypatch.setattr(tv_agent, "_model_classify", _boom)
    result = tv_agent.summarise_report(ended_reason="customer-ended-call",
                                       transcript="Hello, who is this?")
    assert result["outcome"] == VoiceOutcome.ANSWERED_NOT_ENGAGED.value
    assert result["classified_by"] == "fallback"
    assert "model-error" in result["error"]


# ==================================================================== Step 8

def test_push_to_mcp_success_includes_summary_and_recording(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.record_call_outcome_result = {
        "status": "ok", "contact_id": "123", "outcome": "answered_and_engaged",
        "events": [{"probability": 60, "stage": "scheduling", "promoted_to_scheduling": True}],
        "failures": [], "probability": 60, "promoted_to_scheduling": True,
    }
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.push_to_mcp("123", "answered_and_engaged", summary="Great call",
                                  recording_url="https://x/rec.mp3", call_id="call-1")
    assert result["summary"] == "Great call"
    assert result["recording_url"] == "https://x/rec.mp3"
    assert result["probability"] == 60
    call = fake.calls[0]
    assert call[0] == "record_call_outcome"
    assert call[1] == "123" and call[2] == "answered_and_engaged"
    assert "call=call-1" in call[3] and "recording=https://x/rec.mp3" in call[3] and "Great call" in call[3]


def test_push_to_mcp_reports_crm_error_as_error_status_not_exception(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.record_call_outcome_error = CRMError("HubSpot unreachable")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.push_to_mcp("123", "voicemail")
    assert result["status"] == "error"
    assert "crm-error" in result["reason"]


def test_push_to_mcp_surfaces_partial_failures(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.record_call_outcome_result = {
        "status": "partial", "contact_id": "123", "outcome": "voicemail",
        "events": [], "failures": ["crm-error: upsert_lead: boom"],
    }
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.push_to_mcp("123", "voicemail")
    assert result["status"] == "partial"
    assert result["failures"] == ["crm-error: upsert_lead: boom"]


# ============================================================ handle_new_lead

def test_handle_new_lead_stops_at_step_3_when_not_callable(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="COMPLETED")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    place_call_invoked = []
    monkeypatch.setattr(tv_agent, "place_call",
                        lambda lead, lead_context="": place_call_invoked.append(lead))

    result = tv_agent.handle_new_lead("904")
    assert result["status"] == "stopped"
    assert result["step"] == "3"
    assert result["object_id"] == "904"
    assert not place_call_invoked
    # Step 3 stopping the lead must never write voice_status itself — only
    # Step 4's claim (below) does that, and Step 4 never ran.
    assert not any(c[0] == "upsert_lead" for c in fake.calls)


def test_handle_new_lead_claims_initiated_before_dialing(tv_agent, monkeypatch):
    """The core ordering fix: the INITIATED write must happen BEFORE
    place_call runs, not after — both for dedup and so a fast-failing call's
    Step 8 write can never be clobbered by a late Step-4 write landing after it."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    monkeypatch.setattr(tv_agent, "mcp", fake)

    order = []

    def fake_place_call(lead, lead_context=""):
        order.append("place_call")
        return {"status": "initiated", "call_id": "call-1", "to": lead.phone_number}

    monkeypatch.setattr(tv_agent, "place_call", fake_place_call)

    original_upsert = fake.upsert_lead

    def tracking_upsert(*args, **kwargs):
        order.append("upsert_lead")
        return original_upsert(*args, **kwargs)

    fake.upsert_lead = tracking_upsert

    result = tv_agent.handle_new_lead("E1")
    # INITIATED is claimed before the dial; CALL_PLACED lands after Vapi
    # accepts (2026-08-10 state-machine decision).
    assert order[:2] == ["upsert_lead", "place_call"]
    assert result["status"] == "initiated"
    initiated_call = [c for c in fake.calls if c[0] == "upsert_lead"][0]
    assert initiated_call[2] == "INITIATED"


def test_handle_new_lead_dials_anyway_when_initiated_write_fails(tv_agent, monkeypatch):
    """Failing the pre-dial claim degrades dedup protection but must never
    cancel a call to an otherwise-ready lead."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    fake.upsert_results = [CRMError("HubSpot down")]
    monkeypatch.setattr(tv_agent, "mcp", fake)
    dialed = []
    monkeypatch.setattr(tv_agent, "place_call",
                        lambda lead, lead_context="": dialed.append(lead) or {"status": "initiated", "call_id": "c1"})
    result = tv_agent.handle_new_lead("E1")
    assert result["status"] == "initiated"
    assert len(dialed) == 1


def test_handle_new_lead_releases_claim_on_vapi_error(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    monkeypatch.setattr(tv_agent, "mcp", fake)

    def raising_place_call(lead, lead_context=""):
        raise tv_agent.VapiError("Vapi 500 after 3 retries")

    monkeypatch.setattr(tv_agent, "place_call", raising_place_call)

    result = tv_agent.handle_new_lead("E1")
    assert result["status"] == "error"
    assert result["step"] == "4"
    upserts = [c for c in fake.calls if c[0] == "upsert_lead"]
    assert [u[2] for u in upserts] == ["INITIATED", "FAILED"]  # claimed, then released


def test_handle_new_lead_releases_claim_on_unexpected_exception(tv_agent, monkeypatch):
    """Found live via the ADK runtime: a SecretNotFoundError out of
    VapiClient() construction is not a VapiError, so it used to escape
    handle_new_lead entirely with the claim still held — the lead sat
    INITIATED with no call in flight and every retry was refused as a
    duplicate. Any exception out of place_call means no call exists, so the
    claim must be released and the failure reported, not raised."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    monkeypatch.setattr(tv_agent, "mcp", fake)

    def raising_place_call(lead, lead_context=""):
        raise RuntimeError("Secret Manager lookup failed for 'lqabr-vapi-api-key'")

    monkeypatch.setattr(tv_agent, "place_call", raising_place_call)

    result = tv_agent.handle_new_lead("E1")  # must NOT raise
    assert result["status"] == "error"
    assert result["step"] == "4"
    assert "pre-dial failure" in result["reason"]
    upserts = [c for c in fake.calls if c[0] == "upsert_lead"]
    assert [u[2] for u in upserts] == ["INITIATED", "FAILED"]  # claimed, then released


def test_handle_new_lead_releases_claim_when_place_call_returns_error_dict(tv_agent, monkeypatch):
    """place_call's own boundary checks (opted-out, no phone, no phone-number-id
    config) return an error dict rather than raising — this path must release
    the claim too, or the lead is stranded on INITIATED forever."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    monkeypatch.setattr(tv_agent, "mcp", fake)
    monkeypatch.setattr(tv_agent, "place_call",
                        lambda lead, lead_context="": {"error": "opted-out: contact has opted out of outreach"})

    result = tv_agent.handle_new_lead("E1")
    assert result["status"] == "stopped"
    assert result["step"] == "4"
    upserts = [c for c in fake.calls if c[0] == "upsert_lead"]
    assert [u[2] for u in upserts] == ["INITIATED", "FAILED"]


def test_handle_new_lead_success_returns_call_id_and_leaves_claim_in_place(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    monkeypatch.setattr(tv_agent, "mcp", fake)
    monkeypatch.setattr(tv_agent, "place_call",
                        lambda lead, lead_context="": {"status": "initiated", "call_id": "call-9",
                                                      "to": lead.phone_number,
                                                      "lead_context_chars": 0})

    result = tv_agent.handle_new_lead("123")
    assert result == {"status": "initiated", "step": "4",
                      "contact_id": "123", "call_id": "call-9", "to": "+15550001111",
                      "lead_context_chars": 0}
    upserts = [c for c in fake.calls if c[0] == "upsert_lead"]
    # Claim, then advance once Vapi accepted. No rollback on success.
    assert [u[2] for u in upserts] == ["INITIATED", "CALL_PLACED"]


# ================================================================= _release_claim

def test_release_claim_noop_without_contact_id(tv_agent, monkeypatch):
    fake = FakeMCP()
    monkeypatch.setattr(tv_agent, "mcp", fake)
    tv_agent._release_claim(None, "some-reason")
    assert fake.calls == []


def test_release_claim_writes_failed(tv_agent, monkeypatch):
    """FAILED (2026-08-06, user decision): every never-dialed case reports the
    same as a call that was placed and never answered, on purpose — the
    alternative (PENDING) left permanently-blocked leads (opted-out, bad
    phone number) looking like they were still waiting their turn."""
    fake = FakeMCP()
    monkeypatch.setattr(tv_agent, "mcp", fake)
    tv_agent._release_claim("123", "vapi-error: boom")
    assert fake.calls == [("upsert_lead", "123", "FAILED", None, None)]


def test_release_claim_swallows_crm_error_rather_than_raising(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.upsert_results = [CRMError("HubSpot down")]
    monkeypatch.setattr(tv_agent, "mcp", fake)
    tv_agent._release_claim("123", "vapi-error: boom")  # must not raise


# ============================================================= handle_call_report

def test_handle_call_report_stops_when_contact_unresolved(tv_agent, monkeypatch):
    monkeypatch.setattr(tv_agent, "summarise_report",
                        lambda ended_reason, transcript: {
                            "outcome": "voicemail", "summary": "left a message",
                            "classified_by": "ended_reason"})
    monkeypatch.setattr(tv_agent, "_contact_id_for_report", lambda report: None)
    pushed = []
    monkeypatch.setattr(tv_agent, "push_to_mcp", lambda *a, **kw: pushed.append((a, kw)))

    result = tv_agent.handle_call_report({"call": {"id": "call-1"}, "endedReason": "voicemail"})
    assert result["status"] == "stopped"
    assert result["step"] == "8"
    assert not pushed


def test_handle_call_report_runs_step_8_with_resolved_contact(tv_agent, monkeypatch):
    monkeypatch.setattr(tv_agent, "summarise_report",
                        lambda ended_reason, transcript: {
                            "outcome": "answered_and_engaged", "summary": "Great fit",
                            "classified_by": "model"})
    monkeypatch.setattr(tv_agent, "_contact_id_for_report", lambda report: "123")
    monkeypatch.setattr(tv_agent, "push_to_mcp",
                        lambda contact_id, outcome, summary="", recording_url="", call_id="":
                            {"status": "ok", "contact_id": contact_id, "probability": 60,
                             "promoted_to_scheduling": True, "failures": []})

    report = {"call": {"id": "call-1"}, "endedReason": "customer-ended-call",
              "artifact": {"transcript": "hi there", "recordingUrl": "https://x/r.mp3"}}
    result = tv_agent.handle_call_report(report)
    assert result["status"] == "ok"
    assert result["contact_id"] == "123"
    assert result["outcome"] == "answered_and_engaged"
    assert result["probability"] == 60
    assert result["promoted_to_scheduling"] is True


# ======================================================== _contact_id_for_report

def test_contact_id_for_report_reads_top_level_variable_values(tv_agent):
    report = {"assistantOverrides": {"variableValues": {"contact_id": "77"}}}
    assert tv_agent._contact_id_for_report(report) == "77"


def test_contact_id_for_report_reads_call_nested_variable_values(tv_agent):
    report = {"call": {"assistantOverrides": {"variableValues": {"contact_id": "88"}}}}
    assert tv_agent._contact_id_for_report(report) == "88"


def test_contact_id_for_report_falls_back_to_phone_lookup(tv_agent, monkeypatch):
    fake = FakeMCP()
    # A real LeadProfile, not a duck-typed stub: LeadProfile's id field is
    # `object_id` and it has no `contact_id`, so a stub carrying `contact_id`
    # asserted a contract the real object never had (and hid the AttributeError
    # this path used to raise in production).
    from lqabr_core.types import LeadProfile
    fake.find_lead_by_phone_result = LeadProfile(object_id="99")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    report = {"call": {"customer": {"number": "+15550001111"}}}
    assert tv_agent._contact_id_for_report(report) == "99"
    assert fake.calls == [("find_lead_by_phone", "+15550001111")]


def test_contact_id_for_report_returns_none_when_phone_lookup_finds_nothing(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.find_lead_by_phone_result = None
    monkeypatch.setattr(tv_agent, "mcp", fake)
    report = {"customer": {"number": "+15550009999"}}
    assert tv_agent._contact_id_for_report(report) is None


def test_contact_id_for_report_returns_none_when_no_number_at_all(tv_agent, monkeypatch):
    fake = FakeMCP()
    monkeypatch.setattr(tv_agent, "mcp", fake)
    assert tv_agent._contact_id_for_report({}) is None
    assert fake.calls == []  # no phone to look up, no CRM call made


def test_contact_id_for_report_swallows_crm_error_and_returns_none(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.find_lead_by_phone_error = CRMError("HubSpot down")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    report = {"customer": {"number": "+15550001111"}}
    assert tv_agent._contact_id_for_report(report) is None


# ============================================================ list_text_voice_queue

def test_list_text_voice_queue_returns_leads_and_thresholds(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.leads_in_stage_result = [_voice_lead(), _voice_lead(employee_id="E2", contact_id="124")]
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.list_text_voice_queue(limit=10)
    assert result["count"] == 2
    assert result["entry_threshold"] == tv_agent.TEXT_VOICE_THRESHOLD
    assert result["threshold_to_scheduling"] == tv_agent.SCHEDULING_THRESHOLD
    assert len(result["leads"]) == 2


def test_list_text_voice_queue_reports_crm_error_instead_of_crashing(tv_agent, monkeypatch):
    """The un-guarded CRM call this test would have caught if it crashed the
    whole adk session instead of returning an error dict."""
    fake = FakeMCP()
    fake.leads_in_stage_error = CRMError("HubSpot down")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.list_text_voice_queue()
    assert result["count"] == 0
    assert "crm-error" in result["error"]


# =================================================================== _MCPAdapter

class FakeCRM:
    """Stands in for HubSpotClient at the `_request` boundary _MCPAdapter uses
    directly (get contact by id, get company, PATCH properties)."""

    def __init__(self, responses_by_call=None, starting_probability=30):
        self.calls = []
        self._script = list(responses_by_call or [])
        self._probability = starting_probability  # mirrors HubSpotClient.record_event:
                                                    # each call reads current state and
                                                    # compounds, rather than resetting.

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def record_event(self, event):
        self.calls.append(("record_event", event))
        from lqabr_core.probability import apply_event, stage_for_probability
        from lqabr_core.types import LeadProfile
        self._probability = apply_event(self._probability, event.event_type)
        return LeadProfile(object_id=event.contact_id,
                           probability=self._probability,
                           stage=stage_for_probability(self._probability))


def _adapter_with_crm(tv_agent, crm):
    adapter = tv_agent._MCPAdapter()
    adapter._client = crm
    return adapter


def test_mcpadapter_contact_by_id_does_a_single_direct_get(tv_agent):
    """The Agent Gateway now forwards HubSpot's own `objectId` (confirmed
    against HubSpot's custom-workflow-action docs: "the unique system ID for
    the specific contact"), not the external `employee_id` property — so this
    is one direct GET, no idProperty/Search fallback needed anymore."""
    crm = FakeCRM([{"id": "904", "properties": {}, "associations": {}}])
    adapter = _adapter_with_crm(tv_agent, crm)
    contact = adapter._contact_by_id("904")
    assert contact["id"] == "904"
    assert len(crm.calls) == 1
    method, path, kwargs = crm.calls[0]
    assert method == "GET" and path.endswith("/contacts/904")
    assert "idProperty" not in kwargs["params"]  # no fallback plumbing left


def test_mcpadapter_contact_by_id_returns_none_on_404(tv_agent):
    """A 404 means the contact genuinely doesn't exist (e.g. deleted since the
    gateway enrolled it) — reported as not-found, not raised as a CRM error."""
    crm = FakeCRM([CRMError("HubSpot GET /crm/v3/objects/contacts/904 failed: "
                            "HTTP 404: {\"message\": \"not found\"}")])
    adapter = _adapter_with_crm(tv_agent, crm)
    assert adapter._contact_by_id("904") is None


def test_mcpadapter_contact_by_id_reraises_non_404_errors(tv_agent):
    """A 5xx or auth failure is a real CRM error, not a not-found — it must
    propagate rather than being swallowed into a false "no such contact"."""
    import pytest
    crm = FakeCRM([CRMError("HubSpot GET /crm/v3/objects/contacts/904 failed "
                            "after 3 retries: HTTP 500: server error")])
    adapter = _adapter_with_crm(tv_agent, crm)
    with pytest.raises(CRMError):
        adapter._contact_by_id("904")


def test_mcpadapter_to_voice_lead_maps_email_id_fallback_and_opted_out_string(tv_agent):
    contact = {"id": "1", "properties": {
        "firstname": "Jane", "lastname": "Smith", "email_id": "j@x.com",
        "phone": "+1555", "opted_out": "true", "probability": "45",
        "decision_maker": "yes", "lqabr_voice_status": "COMPLETED",
    }}
    company = {"id": "9", "properties": {"industry": "Tech", "name": "Acme"}}
    lead = tv_agent._MCPAdapter._to_voice_lead(contact, company)
    assert lead.full_name == "Jane Smith"
    assert lead.opted_out is True
    assert lead.probability == 45
    assert lead.company_name == "Acme"
    assert lead.hubspot_company_id == "9"


def test_mcpadapter_upsert_lead_rejects_unknown_voice_status(tv_agent):
    import pytest
    crm = FakeCRM([])
    adapter = _adapter_with_crm(tv_agent, crm)
    with pytest.raises(CRMError, match="not one of"):
        adapter.upsert_lead("1", voice_status="BOGUS")


def test_mcpadapter_upsert_lead_derives_voice_status_from_outcome(tv_agent):
    crm = FakeCRM([{}])
    adapter = _adapter_with_crm(tv_agent, crm)
    result = adapter.upsert_lead("1", outcome="voicemail")
    assert result["properties"]["lqabr_voice_status"] == "VOICEMAIL_LEFT"
    # every real write stamps the Text/Voice Agent's own last-touched marker
    assert result["properties"]["last_modfied_voice"].isdigit()


def test_mcpadapter_record_call_outcome_engaged_call_records_two_events(tv_agent):
    crm = FakeCRM([{}])  # one PATCH from upsert_lead
    adapter = _adapter_with_crm(tv_agent, crm)
    result = adapter.record_call_outcome("1", "answered_and_engaged")
    assert result["status"] == "ok"
    event_calls = [c for c in crm.calls if c[0] == "record_event"]
    assert len(event_calls) == 2
    assert event_calls[0][1].event_type is EventType.CALL_ANSWERED
    assert event_calls[1][1].event_type is EventType.CALL_ENGAGED
    assert result["promoted_to_scheduling"] is True  # 30 + 15 + 15 == SCHEDULING_THRESHOLD


def test_mcpadapter_record_call_outcome_reports_partial_on_upsert_failure(tv_agent):
    crm = FakeCRM([CRMError("PATCH failed")])
    adapter = _adapter_with_crm(tv_agent, crm)
    result = adapter.record_call_outcome("1", "voicemail")
    assert result["status"] == "partial"
    assert any("upsert_lead" in f for f in result["failures"])
    # the event is still attempted even though the upsert failed
    assert any(c[0] == "record_event" for c in crm.calls)



# ==========================================================================
# SP-2 FR4 — lead_context
#
# `lead_context` is a real contact property on portal 246777241 (type string,
# label "lead_context", description "Lead context notes"), verified against
# the live properties API on 2026-08-17 rather than read off a UI label. It is
# deliberately NOT a VoiceLead field — VoiceLead lives in packages/lqabr_core,
# which this agent does not own — so it travels as its own value from Step 3
# through handle_new_lead into Step 4.
# ==========================================================================

def test_lead_context_is_in_the_properties_step_3_asks_hubspot_for(tv_agent):
    """A property that isn't in the GET's `properties` list comes back absent,
    silently — the exact failure mode this repo keeps hitting."""
    assert "lead_context" in tv_agent._CONTACT_PROPERTIES


def test_mcpadapter_get_lead_with_extras_reads_it_from_the_same_get(tv_agent):
    crm = FakeCRM([{"id": "904",
                    "properties": {"firstname": "Jane", "phone": "+15550001111",
                                   "lead_context": "Re: cutting your cloud spend"},
                    "associations": {}}])
    adapter = _adapter_with_crm(tv_agent, crm)
    lead, extras = adapter.get_lead_with_extras("904")
    lead_context = extras["lead_context"]
    assert lead.contact_id == "904"
    assert lead_context == "Re: cutting your cloud spend"
    # One GET, not two: no separate round-trip just for the context.
    assert len([c for c in crm.calls if c[0] == "GET"]) == 1


def test_mcpadapter_get_lead_with_extras_returns_empty_when_unset(tv_agent):
    """The common case today — the property exists on every contact in the
    portal and is populated on none of them (HAS_PROPERTY search: 0 of 0,
    2026-08-17)."""
    crm = FakeCRM([{"id": "904", "properties": {"phone": "+15550001111"},
                    "associations": {}}])
    adapter = _adapter_with_crm(tv_agent, crm)
    _, extras = adapter.get_lead_with_extras("904")
    lead_context = extras["lead_context"]
    assert lead_context == ""


def test_mcpadapter_get_lead_with_extras_returns_none_for_a_missing_contact(tv_agent):
    crm = FakeCRM([CRMError("HubSpot GET /crm/v3/objects/contacts/904 failed: "
                            "HTTP 404: {\"message\": \"not found\"}")])
    adapter = _adapter_with_crm(tv_agent, crm)
    lead, extras = adapter.get_lead_with_extras("904")
    assert lead is None and extras["lead_context"] == ""


def test_get_lead_returns_lead_context_for_a_callable_lead(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    fake.lead_context_result = "Re: cutting your cloud spend"
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("904")
    assert result["callable"] is True
    assert result["lead_context"] == "Re: cutting your cloud spend"


def test_get_lead_falls_back_when_the_mcp_tool_surface_has_no_context_reader(
        tv_agent, monkeypatch):
    """`mcp` may resolve to the real Step 5 tool module, which only promises
    the five names in _MCP_TOOL_NAMES. A lead is still callable without
    context — it just opens on the generic intro."""
    class LegacyMCP:
        def __init__(self):
            self.calls = []

        def get_lead(self, object_id):
            self.calls.append(object_id)
            return _voice_lead()

    legacy = LegacyMCP()
    monkeypatch.setattr(tv_agent, "mcp", legacy)
    result = tv_agent.get_lead("904")
    assert result["callable"] is True
    assert result["lead_context"] == ""
    assert legacy.calls == ["904"]


def test_handle_new_lead_hands_lead_context_to_step_4(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    fake.lead_context_result = "Re: cutting your cloud spend"
    monkeypatch.setattr(tv_agent, "mcp", fake)

    seen = {}

    def fake_place_call(lead, lead_context=""):
        seen["lead_context"] = lead_context
        return {"status": "initiated", "call_id": "call-1",
                "to": lead.phone_number, "lead_context_chars": len(lead_context)}

    monkeypatch.setattr(tv_agent, "place_call", fake_place_call)
    result = tv_agent.handle_new_lead("904")
    assert seen["lead_context"] == "Re: cutting your cloud spend"
    assert result["lead_context_chars"] == 28


def test_handle_new_lead_dials_with_empty_context_when_the_property_is_unset(
        tv_agent, monkeypatch):
    """Missing context must never block a call — FR4's fallback branch is the
    normal path in this portal today."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    fake.lead_context_result = ""
    monkeypatch.setattr(tv_agent, "mcp", fake)

    seen = {}

    def fake_place_call(lead, lead_context=""):
        seen["lead_context"] = lead_context
        return {"status": "initiated", "call_id": "call-1",
                "to": lead.phone_number, "lead_context_chars": 0}

    monkeypatch.setattr(tv_agent, "place_call", fake_place_call)
    result = tv_agent.handle_new_lead("904")
    assert result["status"] == "initiated"
    assert seen["lead_context"] == ""


def test_get_lead_puts_lead_context_on_process_log(tv_agent, monkeypatch):
    """User request 2026-08-17: the text itself, not only its length. Step 3
    logs the RAW property value."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    fake.lead_context_result = "Re: cutting your cloud spend"
    monkeypatch.setattr(tv_agent, "mcp", fake)

    logged = {}
    real_step = tv_agent.obs.step

    import contextlib

    @contextlib.contextmanager
    def capturing_step(step_name, **fields):
        with real_step(step_name, **fields) as outcome:
            yield outcome
            logged[step_name] = dict(outcome)

    monkeypatch.setattr(tv_agent.obs, "step", capturing_step)
    tv_agent.get_lead("904")

    entry = logged[tv_agent.obs.STEP_READ_LEAD]
    assert entry["lead_context"] == "Re: cutting your cloud spend"
    assert entry["lead_context_chars"] == 28


def test_place_call_step_logs_the_context_actually_sent_to_vapi(tv_agent, monkeypatch):
    """Step 4 logs the CAPPED value that went on the wire, which can differ
    from the raw property Step 3 read — that difference is the whole point."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    fake.lead_context_result = "the full untruncated raw value from HubSpot"
    monkeypatch.setattr(tv_agent, "mcp", fake)
    monkeypatch.setattr(tv_agent, "place_call",
                        lambda lead, lead_context="": {
                            "status": "initiated", "call_id": "c1",
                            "to": lead.phone_number,
                            "lead_context": "the full untrunc\u2026",   # as capped
                            "lead_context_chars": 17})

    logged = {}
    real_step = tv_agent.obs.step

    import contextlib

    @contextlib.contextmanager
    def capturing_step(step_name, **fields):
        with real_step(step_name, **fields) as outcome:
            yield outcome
            logged[step_name] = dict(outcome)

    monkeypatch.setattr(tv_agent.obs, "step", capturing_step)
    tv_agent.handle_new_lead("904")

    entry = logged[tv_agent.obs.STEP_PLACE_CALL]
    assert entry["lead_context"] == "the full untrunc\u2026"
    assert entry["lead_context_chars"] == 17


def test_contact_id_for_report_uses_leadprofile_object_id_not_contact_id(
        tv_agent, monkeypatch):
    """Regression: LeadProfile (what find_lead_by_phone returns) has
    `object_id` and no `contact_id`. Reading `.contact_id` raised
    AttributeError — not a CRMError, so it escaped this function's guard and
    Step 8 never ran for that call."""
    from lqabr_core.types import LeadProfile
    fake = FakeMCP()
    fake.find_lead_by_phone_result = LeadProfile(object_id="77", phone="+15550001111")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    report = {"customer": {"number": "+15550001111"}}
    assert tv_agent._contact_id_for_report(report) == "77"


# ==========================================================================
# voice_status state machine (decision, Rao, 2026-08-10)
#
#   PENDING -> INITIATED (before the dial) -> CALL_PLACED (Vapi accepted)
#           -> COMPLETED | VOICEMAIL_LEFT | FAILED (end-of-call report)
#
# Rollback on a never-placed call stays FAILED (2026-08-06 decision, upheld
# 2026-08-17) — covered by the existing _release_claim tests above.
# ==========================================================================

def test_call_placed_is_a_valid_status_value(tv_agent):
    assert "CALL_PLACED" in tv_agent._VOICE_STATUS_VALUES
    assert tv_agent._IN_FLIGHT_VOICE_STATUSES == ("INITIATED", "CALL_PLACED")


def test_call_placed_blocks_a_duplicate_dial(tv_agent, monkeypatch):
    """The whole point of splitting the pre-dial state: a redelivered gateway
    request arriving after Vapi accepted must not dial the person again."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="CALL_PLACED")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is False
    assert result["reason"].startswith("in-flight: voice_status=CALL_PLACED")


def test_stale_in_flight_claim_is_released_for_a_redial(tv_agent, monkeypatch):
    """A lead whose end-of-call report never arrived is stranded forever
    without this — INITIATED blocks and nothing clears it."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="INITIATED")
    fake.voice_status_written_ms_result = int(
        (time.time() - 3600) * 1000)          # an hour old, window is 15 min
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is True


def test_fresh_in_flight_claim_is_still_blocked(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="INITIATED")
    fake.voice_status_written_ms_result = int((time.time() - 60) * 1000)
    monkeypatch.setattr(tv_agent, "mcp", fake)
    assert tv_agent.get_lead("E1")["callable"] is False


def test_in_flight_without_a_timestamp_fails_closed(tv_agent, monkeypatch):
    """No last_modfied_voice means we cannot know the age. Blocking is the
    safe direction — the alternative is guessing and dialling a real person."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="INITIATED")
    fake.voice_status_written_ms_result = None
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is False
    assert "cannot age it out" in result["reason"]


def test_a_stale_claim_does_not_excuse_a_missing_phone(tv_agent, monkeypatch):
    """Releasing the in-flight block must not release the other stop
    conditions with it."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="INITIATED", phone_number=None)
    fake.voice_status_written_ms_result = int((time.time() - 3600) * 1000)
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("E1")
    assert result["callable"] is False
    assert result["reason"] == "bad-data: contact has no phone number"


def test_mark_call_placed_skips_when_the_report_already_landed(tv_agent, monkeypatch):
    """The race this guard exists for: a call that fails fast can have its
    end-of-call report — and Step 8's write — land within a second of the
    dial. CALL_PLACED must not overwrite the real outcome."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="COMPLETED")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    assert tv_agent._mark_call_placed("123") == "COMPLETED"
    assert [c for c in fake.calls if c[0] == "upsert_lead"] == []


def test_mark_call_placed_writes_when_still_in_flight(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="INITIATED")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    assert tv_agent._mark_call_placed("123") == "CALL_PLACED"
    assert [c[2] for c in fake.calls if c[0] == "upsert_lead"] == ["CALL_PLACED"]


def test_mark_call_placed_never_raises_when_the_write_fails(tv_agent, monkeypatch):
    """The call is already in flight — losing the marker must not look like a
    failed dial."""
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="INITIATED")
    fake.upsert_results = [CRMError("HubSpot down")]
    monkeypatch.setattr(tv_agent, "mcp", fake)
    assert tv_agent._mark_call_placed("123") == "INITIATED"


def test_epoch_ms_parses_both_shapes_hubspot_uses(tv_agent):
    """We WRITE epoch-ms; HubSpot types the property as a datetime and READS
    it back as ISO 8601. A real GET returned '2026-08-17T14:44:32.633Z'."""
    assert tv_agent._epoch_ms("1786968272633") == 1786968272633
    # Round-trips: 1786977872633 ms == 2026-08-17T14:44:32.633+00:00
    assert tv_agent._epoch_ms("2026-08-17T14:44:32.633Z") == 1786977872633
    assert tv_agent._epoch_ms("") is None
    assert tv_agent._epoch_ms(None) is None
    assert tv_agent._epoch_ms("not a date") is None
