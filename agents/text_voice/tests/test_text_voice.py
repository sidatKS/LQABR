"""Unit tests for agents/text_voice/src/text_voice.py — Steps 3, 7, 8 and both
inbound entrypoints (handle_new_lead, handle_call_report).

Strategy: `mcp` (the module-level Step 5 stand-in) and `place_call` (imported
from tools.py into this module's namespace) are monkeypatched with fakes at
the point they are referenced inside text_voice.py's own globals. This tests
Steps 3/7/8's own logic — dedup/claim ordering, outcome classification,
partial-write reporting — without needing a real HubSpot/Vapi credential or
network call. (The `_MCPAdapter` HubSpot stand-in and its tests were
retired 2026-08-19 — Step 5 is a real MCP server now, see mcp_client.py.)
"""

import time

from lqabr_core.crm.base import CRMError
from lqabr_core.types import VoiceLead, VoiceOutcome


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
        return {"status": "updated", "object_id": object_id}

    def record_call_outcome(self, contact_id, outcome, detail=None, current=None):
        self.calls.append(("record_call_outcome", contact_id, outcome, detail))
        if self.record_call_outcome_error:
            raise self.record_call_outcome_error
        return self.record_call_outcome_result

    def find_lead_by_phone(self, phone):
        self.calls.append(("find_lead_by_phone", phone))
        if self.find_lead_by_phone_error:
            raise self.find_lead_by_phone_error
        return self.find_lead_by_phone_result


def _voice_lead(**overrides):
    base = dict(employee_id="E1", object_id="123", phone_number="+15550001111",
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
    assert result["object_id"] == "123"


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
        "status": "ok", "object_id": "123", "outcome": "answered_and_engaged",
        "events": [{"probability": 60, "stage": "scheduling", "promoted_to_scheduling": True}],
        "failures": [], "probability": 60, "promoted_to_scheduling": True,
    }
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.push_to_mcp("123", "answered_and_engaged", summary="Great call",
                                  recording_url="https://x/rec.mp3")
    assert result["summary"] == "Great call"
    assert result["recording_url"] == "https://x/rec.mp3"
    assert result["probability"] == 60
    call = fake.calls[0]
    assert call[0] == "record_call_outcome"
    assert call[1] == "123" and call[2] == "answered_and_engaged"


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
        "status": "partial", "object_id": "123", "outcome": "voicemail",
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


def test_handle_new_lead_refuses_to_dial_when_the_claim_write_fails(tv_agent, monkeypatch):
    """An unclaimable lead must NOT be dialled.

    The claim is the only thing standing between a redelivered trigger and a
    second call to a real person. Dialling with the guard off means a transient
    CRM error plus an at-least-once redelivery phones the lead twice, so a
    failed claim stops the dial rather than degrading past it.
    """
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    fake.upsert_results = [CRMError("HubSpot down")]
    monkeypatch.setattr(tv_agent, "mcp", fake)
    dialed = []
    monkeypatch.setattr(tv_agent, "place_call",
                        lambda lead, lead_context="": dialed.append(lead) or {"status": "initiated", "call_id": "c1"})
    result = tv_agent.handle_new_lead("E1")
    assert result["status"] == "stopped"
    assert "could not claim" in result["reason"]
    assert dialed == []          # the phone never rang


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
                      "object_id": "123", "call_id": "call-9", "to": "+15550001111",
                      "lead_context_chars": 0}
    upserts = [c for c in fake.calls if c[0] == "upsert_lead"]
    # Claim, then advance once Vapi accepted. No rollback on success.
    assert [u[2] for u in upserts] == ["INITIATED", "CALL_PLACED"]


# ================================================================= _release_claim

def test_release_claim_noop_without_object_id(tv_agent, monkeypatch):
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
    assert fake.calls == [("upsert_lead", "123", "FAILED", None, None, None)]


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
    monkeypatch.setattr(tv_agent, "_object_id_for_report", lambda report: None)
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
    monkeypatch.setattr(tv_agent, "_object_id_for_report", lambda report: "123")
    monkeypatch.setattr(tv_agent, "push_to_mcp",
                        lambda contact_id, outcome, summary="", recording_url="",
                               current=None:
                            {"status": "ok", "contact_id": contact_id, "probability": 60,
                             "promoted_to_scheduling": True, "failures": []})

    report = {"call": {"id": "call-1"}, "endedReason": "customer-ended-call",
              "artifact": {"transcript": "hi there", "recordingUrl": "https://x/r.mp3"}}
    result = tv_agent.handle_call_report(report)
    assert result["status"] == "ok"
    assert result["object_id"] == "123"
    assert result["outcome"] == "answered_and_engaged"
    assert result["probability"] == 60
    assert result["promoted_to_scheduling"] is True


# ======================================================== _object_id_for_report

def test_object_id_for_report_reads_top_level_variable_values(tv_agent):
    report = {"assistantOverrides": {"variableValues": {"object_id": "77"}}}
    assert tv_agent._object_id_for_report(report) == "77"


def test_object_id_for_report_reads_call_nested_variable_values(tv_agent):
    report = {"call": {"assistantOverrides": {"variableValues": {"object_id": "88"}}}}
    assert tv_agent._object_id_for_report(report) == "88"


def test_object_id_for_report_returns_none_when_phone_lookup_finds_nothing(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.find_lead_by_phone_result = None
    monkeypatch.setattr(tv_agent, "mcp", fake)
    report = {"customer": {"number": "+15550009999"}}
    assert tv_agent._object_id_for_report(report) is None


def test_object_id_for_report_returns_none_when_no_number_at_all(tv_agent, monkeypatch):
    fake = FakeMCP()
    monkeypatch.setattr(tv_agent, "mcp", fake)
    assert tv_agent._object_id_for_report({}) is None
    assert fake.calls == []  # no phone to look up, no CRM call made


def test_object_id_for_report_swallows_crm_error_and_returns_none(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.find_lead_by_phone_error = CRMError("HubSpot down")
    monkeypatch.setattr(tv_agent, "mcp", fake)
    report = {"customer": {"number": "+15550001111"}}
    assert tv_agent._object_id_for_report(report) is None


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

def test_get_lead_returns_lead_context_for_a_callable_lead(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead()
    fake.lead_context_result = "Re: cutting your cloud spend"
    monkeypatch.setattr(tv_agent, "mcp", fake)
    result = tv_agent.get_lead("904")
    assert result["callable"] is True
    assert result["lead_context"] == "Re: cutting your cloud spend"


def test_get_lead_raises_loudly_when_the_mcp_tool_surface_has_no_context_reader(
        tv_agent, monkeypatch):
    """2026-08-18: the getattr fallback onto plain `get_lead` was removed.
    `_MCP_TOOL_NAMES` (what flips `_resolve_mcp()` onto a real Step 5 module)
    was never going to include `get_lead_with_extras` anyway, so the old
    fallback wasn't protecting against a real future swap — it was turning a
    broken lead_context into a silent empty string instead of a loud
    failure. An `mcp` object missing the method should now raise, not
    degrade quietly."""
    class LegacyMCP:
        def __init__(self):
            self.calls = []

        def get_lead(self, object_id):
            self.calls.append(object_id)
            return _voice_lead()

    legacy = LegacyMCP()
    monkeypatch.setattr(tv_agent, "mcp", legacy)
    import pytest
    with pytest.raises(AttributeError):
        tv_agent.get_lead("904")
    assert legacy.calls == []  # never reached get_lead(); failed on the attribute lookup first


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
    # (The client-side _VOICE_STATUS_VALUES enum guard retired with the
    # adapter on 2026-08-19 — the MCP server validates status values now.)
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


def test_fresh_in_flight_claim_is_still_blocked(tv_agent, monkeypatch):
    fake = FakeMCP()
    fake.get_lead_result = _voice_lead(voice_status="INITIATED")
    fake.voice_status_written_ms_result = int((time.time() - 60) * 1000)
    monkeypatch.setattr(tv_agent, "mcp", fake)
    assert tv_agent.get_lead("E1")["callable"] is False


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
