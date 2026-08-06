"""Rev 5 STEPS 3, 7 and 8 — the model and its tools.

    STEP 3  get_lead()          Read the lead from the MCP folder before dialing.
    STEP 4  (delegated)         tools.place_call() — the one outbound leg.
    STEP 7  summarise_report()  The agent reads transcript + endedReason and
                                decides what actually happened on the call.
    STEP 8  push_to_mcp()       Write the outcome back so HubSpot reflects it.

    adk web  agents/text_voice/src
    adk run  agents/text_voice/src
    adk api_server agents/text_voice/src    # Cloud Run

Two entrypoints, one per inbound route
--------------------------------------
`handle_new_lead(object_id)` runs Steps 3 → 4 in one invocation, and
`handle_call_report(report)` runs Steps 7 → 8 in the next one. They are
separated by the call itself, which belongs entirely to Vapi (Step 6) and can
last minutes — so this agent holds no state between them. Everything Step 7
needs arrives in the report; everything Step 8 needs is derived from it plus a
CRM lookup. HubSpot remains the only system of record.

Why Step 7 calls the model directly
-----------------------------------
Rev 5 is explicit that the model — not a prebuilt Vapi analysisPlan — decides
the outcome, and that process_log must carry "which model was invoked, input
tokens, output tokens, and latency per call". Both requirements point at one
implementation: this module makes the completion call itself rather than
leaving classification to whatever wraps it. That also means Step 7 works
identically whether it was reached from an `adk` session or from a background
task on the webhook service, which is the path that actually runs in
production.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

from lqabr_core import observability as obs
from lqabr_core.crm import hubspot as _hubspot
from lqabr_core.crm.base import CRMError
from lqabr_core.probability import SCHEDULING_THRESHOLD, TEXT_VOICE_THRESHOLD
from lqabr_core.types import (EngagementEvent, EventType, LeadStage, VoiceLead,
                              VoiceOutcome)

try:
    from .tools import VapiError, place_call
except ImportError:  # pragma: no cover - `adk run` puts src/ on sys.path
    from tools import VapiError, place_call  # type: ignore

logger = logging.getLogger("lqabr.text_voice.agent")

MODEL = os.environ.get("LQABR_TEXT_VOICE_MODEL", "anthropic/claude-sonnet-5")

# The email_status values that satisfy the Rev 5 trigger. Config-overridable
# so a future CLICKED option (decision item #17) is an env change, not a code
# edit: LQABR_QUALIFIED_EMAIL_STATUSES="OPENED,CLICKED".
QUALIFIED_EMAIL_STATUSES = frozenset(
    s.strip().upper() for s in
    os.environ.get("LQABR_QUALIFIED_EMAIL_STATUSES", "OPENED").split(",")
    if s.strip())

# Rev 5 Step 1 eligibility (`lqabr_email_status` reaching OPENED) is enforced
# entirely by the HubSpot workflow that fires the Agent Gateway — by the time a
# lead reaches this service it has already passed that gate, so nothing here
# re-checks it. `lqabr_email_status` is still read onto the VoiceLead because it
# is part of the Rev 5 field set, it is simply not branched on.
#
# Note for anyone reading the spec alongside this: Rev 5 words the trigger as
# 'email_status is set to "clicked"', but the real `lqabr_email_status`
# enumeration in portal 246777241 has no CLICKED option (PENDING, SENT,
# DELIVERED, OPENED, FAILED, BOUNCED), so OPENED is the value in use.


# ==========================================================================
# TEMPORARY — Step 5 MCP adapter
#
# Rev 5 puts the read/write tools in packages/lqabr_core/lqabr_core/crm/
# hubspot.py as module-level functions (the "STEP 5 · CENTRAL MCP FOLDER" box).
# That file is currently owned by another developer, so this agent does NOT
# edit it. Everything below satisfies the same contract on top of the
# HubSpotClient that exists today, and `_resolve_mcp()` prefers the real tools
# the moment they land — at which point this whole block is deleted and nothing
# else in this file changes.
#
# The contract the agent depends on (see docs/REV5_MCP_TOOL_CONTRACT.md):
#     get_lead(object_id)                  -> VoiceLead | None
#     upsert_lead(contact_id, voice_status=, probability=, outcome=)  -> dict
#     record_call_outcome(contact_id, outcome, detail=)              -> dict
#     find_lead_by_phone(phone)            -> LeadProfile | None
#     leads_in_stage(stage, limit=)        -> list[LeadProfile]
# ==========================================================================

_MCP_TOOL_NAMES = ("get_lead", "upsert_lead", "record_call_outcome",
                   "find_lead_by_phone", "leads_in_stage")

# Contact properties Step 3 reads. Names verified against portal 246777241 on
# 2026-07-30 via GET /crm/v3/properties/contacts rather than trusted from the
# labels in the HubSpot UI: plain `voice_status` and `email_status` come back as
# propertiesNotFound — the real properties are the `lqabr_`-prefixed ones that
# carry those labels.
_CONTACT_PROPERTIES = (
    "firstname", "lastname", "jobtitle", "company", "email", "email_id", "phone",
    "employee_id", "decision_maker", "opted_out", "probability",
    "lqabr_voice_status", "lqabr_email_status",
)

# Company properties Step 3 reads from the associated company. Verified the
# same way: `industry` and `annualrevenue` are standard, `company_id` and
# `frequency_of_purchase` are real custom properties on this portal.
_COMPANY_PROPERTIES = ("company_id", "industry", "annualrevenue",
                       "frequency_of_purchase", "name")

# The real option list of the `lqabr_voice_status` enumeration. Writing anything
# else is a 400 from HubSpot, so it is rejected here first with a message that
# names the valid values.
_VOICE_STATUS_VALUES = ("PENDING", "INITIATED", "COMPLETED", "FAILED",
                        "VOICEMAIL_LEFT")

# Step 7 outcome -> voice_status. Only five enum options exist, so both
# answered outcomes land on COMPLETED — the call happened either way. What
# separates them is probability (see _EVENTS_FOR_OUTCOME).
_VOICE_STATUS_FOR_OUTCOME = {
    VoiceOutcome.NOT_ANSWERED.value: "FAILED",
    VoiceOutcome.VOICEMAIL.value: "VOICEMAIL_LEFT",
    VoiceOutcome.ANSWERED_NOT_ENGAGED.value: "COMPLETED",
    VoiceOutcome.ANSWERED_AND_ENGAGED.value: "COMPLETED",
}

# Step 7 outcome -> the engagement events Step 8 records, in order.
# An engaged call is TWO events, not one. probability.py is built so
# 30 (text/voice entry) + 15 (answered) + 15 (engaged) == 60 ==
# SCHEDULING_THRESHOLD; recording only CALL_ENGAGED leaves an engaged lead at
# 45 and it never promotes to the Scheduling Agent.
_EVENTS_FOR_OUTCOME = {
    VoiceOutcome.NOT_ANSWERED.value: (EventType.CALL_NOT_ANSWERED,),
    VoiceOutcome.VOICEMAIL.value: (EventType.VOICEMAIL_LEFT,),
    VoiceOutcome.ANSWERED_NOT_ENGAGED.value: (EventType.CALL_ANSWERED,),
    VoiceOutcome.ANSWERED_AND_ENGAGED.value: (EventType.CALL_ANSWERED,
                                              EventType.CALL_ENGAGED),
}


class _MCPAdapter:
    """Stand-in for the Step 5 tool surface, over today's HubSpotClient.

    Deliberately thin: it maps fields and sequences writes, and delegates all
    HTTP, retry and audit behaviour to HubSpotClient. It reaches
    `HubSpotClient._request` for the two reads the public interface does not
    cover (a contact by id, and a company by id) — a private call that is
    acceptable only because this class is scheduled for deletion, and is the
    reason those two reads belong in hubspot.py rather than here.
    """

    def __init__(self) -> None:
        self._client: Optional[Any] = None

    def _crm(self) -> Any:
        """Built on first use, never at import: constructing it resolves the
        HubSpot token, and importing this module must not require one."""
        if self._client is None:
            self._client = _hubspot.HubSpotClient()
        return self._client

    # ------------------------------------------------------------ read
    def get_lead(self, object_id: str) -> Optional[VoiceLead]:
        contact = self._contact_by_id(object_id)
        if contact is None:
            return None
        return self._to_voice_lead(contact, self._primary_company(contact))

    def _contact_by_id(self, object_id: str) -> Optional[Dict[str, Any]]:
        """One GET by HubSpot's own contact id.

        The Agent Gateway forwards the enrolled record's real `objectId` —
        HubSpot's own field for "the unique system ID for the specific
        contact" (developers.hubspot.com's custom-workflow-action reference),
        not the external `employee_id` property this used to look up by. That
        replaces what used to be an idProperty-GET with a Search fallback
        (needed only because all we had was the external property value,
        which HubSpot's idProperty GET 400s on unless it happens to be
        unique-valued) with a single direct GET: the id we're given is already
        HubSpot's canonical one, so there is nothing left to fall back to.
        A 404 means the contact genuinely doesn't exist (e.g. deleted since
        the gateway enrolled it) and is reported as not-found, not a CRM
        error; anything else (5xx, auth failure) still raises.
        """
        crm = self._crm()
        properties = ",".join(_CONTACT_PROPERTIES)
        try:
            return crm._request(
                "GET", f"/crm/v3/objects/contacts/{quote(str(object_id), safe='')}",
                params={"properties": properties, "associations": "companies"})
        except CRMError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def _primary_company(self, contact: Dict[str, Any]) -> Dict[str, Any]:
        """The first associated company, or {}.

        `associations` returns IDs only, never the associated record's
        properties, so this is always a second call. Failure is swallowed on
        purpose: Step 3's stop conditions are "no contact" and "no phone", so a
        company read that fails must degrade the personalization rather than
        cancel a call.
        """
        refs = ((contact.get("associations") or {}).get("companies") or {})
        results = refs.get("results") or []
        company_id = results[0].get("id") if results else None
        if not company_id:
            return {}
        try:
            return self._crm()._request(
                "GET", f"/crm/v3/objects/companies/{company_id}",
                params={"properties": ",".join(_COMPANY_PROPERTIES)})
        except CRMError:
            obs.log_process(obs.STEP_READ_LEAD, "degraded",
                            "associated company unreadable — continuing without "
                            "company personalization",
                            level=logging.WARNING, hubspot_company_id=company_id)
            return {"id": company_id}

    @staticmethod
    def _to_voice_lead(contact: Dict[str, Any], company: Dict[str, Any]) -> VoiceLead:
        c = contact.get("properties") or {}
        co = company.get("properties") or {}
        full_name = " ".join(x for x in (c.get("firstname"), c.get("lastname")) if x) or None
        try:
            probability = int(float(c.get("probability") or 0))
        except (TypeError, ValueError):
            probability = 0
        return VoiceLead(
            employee_id=c.get("employee_id"),
            # Some contacts (enrichment-sourced leads) hold the address only in
            # the custom `email_id` property, not the standard `email` one.
            email_id=c.get("email_id") or c.get("email"),
            phone_number=c.get("phone"),
            job_title=c.get("jobtitle"),
            decision_maker=c.get("decision_maker"),
            email_status=c.get("lqabr_email_status"),
            voice_status=c.get("lqabr_voice_status"),
            probability=probability,
            company_id=co.get("company_id"),
            industry=co.get("industry"),
            annual_revenue=co.get("annualrevenue"),
            frequency_of_purchase=co.get("frequency_of_purchase"),
            contact_id=contact.get("id"),
            hubspot_company_id=company.get("id"),
            full_name=full_name,
            company_name=co.get("name") or c.get("company"),
            # `opted_out` is a string property in this portal, not a boolean.
            opted_out=str(c.get("opted_out") or "").strip().lower()
            in ("true", "yes", "1"),
        )

    # ----------------------------------------------------------- write
    def upsert_lead(self, contact_id: str, voice_status: Optional[str] = None,
                    probability: Optional[int] = None,
                    outcome: Optional[str] = None) -> Dict[str, Any]:
        if outcome is not None and voice_status is None:
            voice_status = _VOICE_STATUS_FOR_OUTCOME.get(outcome)
            if voice_status is None:
                raise CRMError(f"unknown Step 7 outcome {outcome!r}; expected one "
                               f"of {tuple(_VOICE_STATUS_FOR_OUTCOME)}")
        properties: Dict[str, Any] = {}
        if voice_status is not None:
            if voice_status not in _VOICE_STATUS_VALUES:
                raise CRMError(f"voice_status {voice_status!r} is not one of the "
                               f"lqabr_voice_status values {_VOICE_STATUS_VALUES}")
            properties["lqabr_voice_status"] = voice_status
        if probability is not None:
            properties["probability"] = str(probability)
        if not properties:
            return {"status": "noop", "contact_id": contact_id}
        # `last_modfied_voice` (real HubSpot API name — the property's own
        # label has the same typo, confirmed via the portal's property
        # definition rather than assumed from the UI) is stamped on every
        # write this agent makes, so the contact record shows when it was
        # last touched by the Text/Voice Agent specifically (2026-08-06, user
        # request).
        properties["last_modfied_voice"] = str(int(time.time() * 1000))
        self._crm()._request("PATCH", f"/crm/v3/objects/contacts/{contact_id}",
                             json={"properties": properties})
        return {"status": "updated", "contact_id": contact_id,
                "properties": properties}

    def record_event(self, contact_id: str, event_type: EventType,
                     detail: Optional[str] = None) -> Dict[str, Any]:
        """Delegates to HubSpotClient.record_event, which already applies the
        increment from lqabr_core.probability and writes voice_status. The
        increment is never a number written at this call site."""
        lead = self._crm().record_event(EngagementEvent(
            event_type=event_type, contact_id=contact_id, detail=detail))
        return {"status": "recorded", "contact_id": contact_id,
                "event_type": event_type.value, "probability": lead.probability,
                "stage": lead.stage.value,
                "promoted_to_scheduling": lead.probability >= SCHEDULING_THRESHOLD}

    def record_call_outcome(self, contact_id: str, outcome: str,
                            detail: Optional[str] = None) -> Dict[str, Any]:
        """Rev 5 Step 8, both writes, in order: upsert_lead then record_event.

        Every event is attempted even if an earlier write failed, and failures
        come back in the result rather than as an exception — a partial Step 8
        has to stay visible, because "voice_status written, probability not" is
        a materially different state to recover from than "nothing written".
        """
        events = _EVENTS_FOR_OUTCOME.get(outcome)
        if events is None:
            raise CRMError(f"unknown Step 7 outcome {outcome!r}; expected one of "
                           f"{tuple(_EVENTS_FOR_OUTCOME)}")

        result: Dict[str, Any] = {"contact_id": contact_id, "outcome": outcome,
                                  "events": [], "failures": []}
        try:
            result["upsert"] = self.upsert_lead(contact_id, outcome=outcome)
        except CRMError as exc:
            result["failures"].append(f"crm-error: upsert_lead: {exc}")

        for event_type in events:
            try:
                result["events"].append(
                    self.record_event(contact_id, event_type, detail=detail))
            except CRMError as exc:
                result["failures"].append(
                    f"crm-error: record_event({event_type.value}): {exc}")

        if result["events"]:
            last = result["events"][-1]
            result["probability"] = last["probability"]
            result["stage"] = last["stage"]
            result["promoted_to_scheduling"] = last["promoted_to_scheduling"]
        result["status"] = "partial" if result["failures"] else "ok"
        return result

    # ------------------------------------------------------ passthroughs
    def find_lead_by_phone(self, phone: str) -> Any:
        return self._crm().find_lead_by_phone(phone)

    def leads_in_stage(self, stage: LeadStage, limit: int = 100) -> Any:
        return self._crm().leads_in_stage(stage, limit=limit)


def _resolve_mcp() -> Any:
    """The real Step 5 tools if hubspot.py exposes them, else the adapter."""
    if all(hasattr(_hubspot, name) for name in _MCP_TOOL_NAMES):
        return _hubspot
    return _MCPAdapter()


mcp = _resolve_mcp()


# ==========================================================================
# STEP 3 — Read the Lead
# ==========================================================================

def get_lead(object_id: str) -> Dict[str, Any]:
    """STEP 3 — fetch the lead by its HubSpot object id and decide whether to
    dial.

    `object_id` is HubSpot's own internal id for the contact (the `objectId`
    the Agent Gateway forwards — see the module docstring), not the external
    `employee_id` property this used to look up by. It's named `object_id`
    (not `contact_id`) up through this read specifically because we haven't
    confirmed yet that it resolves to a real contact — once it does, the
    result carries `lead.contact_id` instead, which is what the rest
    of this file (Steps 4/7/8) calls `contact_id`.

    Calls the shared MCP read tool (an in-process package import, not a
    network hop) and applies Rev 5's stop conditions: if no contact is found,
    or the phone number is missing, this step stops and reports the failure and
    Step 4 never runs.

    Two checks beyond the spec's two, both deliberate:
      - `opted_out`. A real field on the contact, and calling someone who has
        opted out is a compliance failure rather than a bug.
      - already-complete. A voice_status of COMPLETED or VOICEMAIL_LEFT means
        this lead's call already happened; a duplicate webhook delivery must
        not dial a person twice.

    Returns the lead plus a `callable` verdict and, when it is False, an
    explicit `reason`. A CRM failure is reported as `crm-error:` — never
    swallowed, and never allowed to look like "no such lead", because those
    two lead to opposite decisions.
    """
    with obs.step(obs.STEP_READ_LEAD, object_id=object_id) as outcome:
        try:
            lead = mcp.get_lead(object_id)
        except CRMError as exc:
            outcome["status"] = "error"
            return {"callable": False, "reason": f"crm-error: {exc}",
                    "object_id": object_id}

        if lead is None:
            outcome["status"] = "stopped"
            outcome["reason"] = "not-found"
            return {"callable": False,
                    "reason": f"not-found: no HubSpot contact with id "
                              f"{object_id}",
                    "object_id": object_id}

        obs.bind(contact_id=lead.contact_id)

        # Defense-in-depth on the Rev 5 trigger (added 2026-07-31, user
        # decision). The HubSpot workflow is the PRIMARY enforcement of
        # `lqabr_email_status=OPENED` — but the workflow isn't configured yet
        # and /voice_agent/lead has no auth, so until both are locked down, trusting
        # upstream alone means any POSTed objectId dials a real phone. Keep
        # QUALIFIED_EMAIL_STATUSES in sync with the workflow's trigger (and
        # with decision item #17 if CLICKED is ever added).
        if (lead.email_status or "").upper() not in QUALIFIED_EMAIL_STATUSES:
            reason = (f"not-qualified: email_status="
                      f"{lead.email_status or 'unset'} (trigger requires one "
                      f"of {sorted(QUALIFIED_EMAIL_STATUSES)})")
            outcome["status"] = "stopped"
            outcome["reason"] = reason
            return {"callable": False, "reason": reason, "lead": lead.to_dict(),
                    "contact_id": lead.contact_id}

        reason = lead.blocking_reason()

        if reason:
            outcome["status"] = "stopped"
            outcome["reason"] = reason
            return {"callable": False, "reason": reason, "lead": lead.to_dict(),
                    "contact_id": lead.contact_id}

        outcome["probability"] = lead.probability
        return {"callable": True, "lead": lead.to_dict(),
                "contact_id": lead.contact_id}


# ==========================================================================
# STEP 7 — Summarise the Report
# ==========================================================================

# endedReason values that settle the outcome on their own — the call never
# reached a conversation, so there is nothing for a model to interpret and
# spending a completion on them would be waste. Every string here is a real
# value of Vapi's endedReason enumeration.
_TERMINAL_ENDED_REASONS: Dict[str, VoiceOutcome] = {
    "customer-did-not-answer": VoiceOutcome.NOT_ANSWERED,
    "customer-busy": VoiceOutcome.NOT_ANSWERED,
    "twilio-failed-to-connect-call": VoiceOutcome.NOT_ANSWERED,
    "vonage-failed-to-connect-call": VoiceOutcome.NOT_ANSWERED,
    "vonage-rejected": VoiceOutcome.NOT_ANSWERED,
    "customer-did-not-give-microphone-permission": VoiceOutcome.NOT_ANSWERED,
    # No LLM available to run the pipeline — an infra failure, not a family of
    # reasons, so it lands here as one exact value rather than in
    # _FAILURE_PREFIXES below. Verified 2026-07-30 against Vapi's documented
    # endedReason enum (docs.vapi.ai/calls/call-ended-reason): this exact
    # string exists and nothing else starts with "pipeline-error-", which is
    # what this codebase used to (wrongly) assume.
    "pipeline-no-available-llm-model": VoiceOutcome.NOT_ANSWERED,
    "voicemail": VoiceOutcome.VOICEMAIL,
}

# Prefixes marking a call that failed on our side or a provider's, rather than
# a lead who did not answer. Recorded as not-answered because that is the
# lead-facing truth (no conversation happened) — but the reason travels with
# it, so a spike of these is visible as infrastructure trouble rather than
# being written off as unresponsive leads.
#
# Verified 2026-07-30 against Vapi's full documented endedReason enum
# (docs.vapi.ai/calls/call-ended-reason and the raw source list on
# github.com/VapiAI/docs). Three prefixes used to live here and were removed
# because no real value ever starts with them:
#   "pipeline-error-"       — the real value is "pipeline-no-available-llm-model"
#                             (no "error" in it — see _TERMINAL_ENDED_REASONS)
#                             or nested as "call.in-progress.error-pipeline-*",
#                             already caught by "call.in-progress.error-" below.
#   "error-vapifault-"      — every real value is nested as
#   "error-providerfault-"    "call.in-progress.error-vapifault-*" /
#                             "call.in-progress.error-providerfault-*" — never
#                             bare, so these two never matched anything.
# "assistant-request-failed" was widened to "assistant-request-": Vapi
# documents five sibling failures (assistant-request-returned-error,
# -returned-unspeakable-error, -returned-invalid-assistant,
# -returned-no-assistant, -returned-forwarding-phone-number) that are the same
# failure mode — the assistant config never resolved — and the narrower
# literal missed all five of them.
_FAILURE_PREFIXES = ("call.start.error-", "call-start-error-",
                     "call.in-progress.error-", "assistant-not-",
                     "assistant-request-")

_SUMMARY_INSTRUCTION = """\
You are analysing one completed outbound sales call.

Decide which ONE of these four outcomes describes what actually happened:

- "not_answered": nobody ever picked up, or the call failed to connect. No
  conversation took place.
- "voicemail": a voicemail system or answering machine picked up, not a person.
  A transcript that is only a recorded greeting ("...is not available, please
  leave a message after the tone") is voicemail, not an answered call.
- "answered_not_engaged": a real person answered, but did not agree to
  continue, was not interested, asked to be called another time, or asked to be
  removed from the list.
- "answered_and_engaged": a real person answered AND showed genuine interest —
  they answered the qualifying questions and/or agreed to receive a calendar
  link or a follow-up.

Judge from what was actually said. The endedReason is a hint about how the
call terminated, not a verdict about whether a human engaged: a call can end
because the customer hung up and still have been fully engaged.

Then write a summary of two or three sentences: what was discussed or decided,
any objection or commitment, and anything a human rep would need to know before
following up. If nothing was discussed, say so plainly instead of inventing
detail. Never state anything that is not in the transcript.

Reply with ONLY a JSON object, no code fence and no other text:
{"outcome": "<one of the four>", "summary": "<your summary>"}
"""


def _litellm_model_name(model_name: str) -> str:
    """Map this repo's model config onto a name litellm accepts.

    `LQABR_<AGENT>_MODEL` holds names in ADK's vocabulary, where a bare
    `gemini-*` is valid because ADK handles Gemini natively. Calling litellm
    directly needs the provider prefix, so a bare gemini name gets one.
    Anything already prefixed (`anthropic/...`, `openai/...`) passes through —
    switching provider stays a config change, never a code edit.
    """
    if "/" in model_name:
        return model_name
    if model_name.startswith("gemini"):
        return f"gemini/{model_name}"
    return model_name


def _classify_from_ended_reason(ended_reason: str) -> Optional[VoiceOutcome]:
    """The outcome when endedReason alone settles it, else None."""
    reason = (ended_reason or "").strip()
    if reason in _TERMINAL_ENDED_REASONS:
        return _TERMINAL_ENDED_REASONS[reason]
    if any(reason.startswith(prefix) for prefix in _FAILURE_PREFIXES):
        return VoiceOutcome.NOT_ANSWERED
    return None


def _deterministic_summary(ended_reason: str, transcript: str,
                           outcome: VoiceOutcome) -> str:
    """A factual summary for the paths that never involve the model."""
    if outcome is VoiceOutcome.NOT_ANSWERED:
        return (f"Call did not connect to a person (endedReason: "
                f"{ended_reason or 'unknown'}). No conversation took place.")
    if outcome is VoiceOutcome.VOICEMAIL:
        return (f"Call reached a voicemail system (endedReason: "
                f"{ended_reason or 'unknown'}); the voicemail message was left. "
                "No live conversation took place.")
    spoken = len((transcript or "").split())
    return (f"Call was answered and ran for roughly {spoken} transcribed words "
            f"(endedReason: {ended_reason or 'unknown'}). Outcome classified "
            "without the model — see process_log for why.")


def summarise_report(ended_reason: str = "", transcript: str = "") -> Dict[str, Any]:
    """STEP 7 — decide the outcome and write the summary.

    The model reads the transcript and endedReason together and classifies the
    call, rather than trusting a prebuilt Vapi analysisPlan. Two paths skip the
    model on purpose:

      - endedReason already settles it (`customer-did-not-answer`, `voicemail`,
        a provider failure). No conversation happened, so there is nothing to
        interpret.
      - an answered call with an empty transcript. Words are the only evidence
        that a person was there; without them, this is treated as voicemail
        rather than credited as an answered call.

    If the model call fails, this falls back to a deterministic classification
    and says so in the result and the log. Step 8 must still run — losing the
    nuance of a summary is acceptable, losing the entire record of a call that
    happened is not.

    Returns {"outcome", "summary", "classified_by", "ended_reason"}.
    """
    transcript = (transcript or "").strip()
    with obs.step(obs.STEP_SUMMARISE, ended_reason=ended_reason,
                  transcript_chars=len(transcript)) as step_result:

        terminal = _classify_from_ended_reason(ended_reason)
        if terminal is not None:
            step_result["outcome"] = terminal.value
            step_result["classified_by"] = "ended_reason"
            return {"outcome": terminal.value,
                    "summary": _deterministic_summary(ended_reason, transcript, terminal),
                    "classified_by": "ended_reason", "ended_reason": ended_reason}

        if not transcript:
            # This is the Twilio-era bug fixed structurally: a connected call
            # with nothing said is not an answered call. Under the old code an
            # AnsweredBy of "unknown" was recorded as CALL_ANSWERED at connect
            # time, which credited +15 probability to calls where nobody ever
            # spoke. Real speech is now the only thing that earns it.
            step_result["outcome"] = VoiceOutcome.VOICEMAIL.value
            step_result["classified_by"] = "empty_transcript"
            return {"outcome": VoiceOutcome.VOICEMAIL.value,
                    "summary": ("Call connected but no speech was transcribed, so "
                                "no person was confirmed on the line "
                                f"(endedReason: {ended_reason or 'unknown'})."),
                    "classified_by": "empty_transcript",
                    "ended_reason": ended_reason}

        try:
            decided = _model_classify(ended_reason, transcript)
        except Exception as exc:  # noqa: BLE001 - never block Step 8
            fallback = VoiceOutcome.ANSWERED_NOT_ENGAGED
            obs.log_process(obs.STEP_SUMMARISE, "degraded",
                            "model classification failed — falling back to a "
                            "deterministic outcome so Step 8 still runs",
                            level=logging.ERROR, error=str(exc)[:300],
                            model=MODEL)
            step_result["outcome"] = fallback.value
            step_result["classified_by"] = "fallback"
            return {"outcome": fallback.value,
                    "summary": _deterministic_summary(ended_reason, transcript, fallback),
                    "classified_by": "fallback", "ended_reason": ended_reason,
                    "error": f"model-error: {exc}"}

        step_result["outcome"] = decided["outcome"]
        step_result["classified_by"] = "model"
        decided["ended_reason"] = ended_reason
        return decided


def _model_classify(ended_reason: str, transcript: str) -> Dict[str, Any]:
    """One completion: classify the call and summarise it.

    Token counts and latency are logged to process_log for every invocation —
    Rev 5 names Step 7's model activity specifically, and without them "what
    did the model decide, and at what cost" is unanswerable after the fact.
    """
    import litellm  # imported here so `adk`-only environments still import this module

    model = _litellm_model_name(MODEL)

    # Provider keys are ENV-ONLY by decision (2026-07-31), same policy as the
    # Vapi key — no Secret Manager lookup on the model path. But litellm reads
    # each provider's OWN standard env var (ANTHROPIC_API_KEY for Anthropic),
    # while ops mounts keys under the lqabr-* convention (LQABR_ANTHROPIC_API_KEY,
    # same name as the Secret Manager entry). model.py.build_model() bridges that
    # gap, but ONLY on the `adk web`/`adk run` path; this webhook path calls
    # litellm directly, so without the bridge here a key mounted as LQABR_* is
    # never seen and Step 7 silently falls back to the deterministic outcome.
    _provider = model.split("/", 1)[0]
    _std_key = {"anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY"}.get(_provider)
    if _std_key and not os.environ.get(_std_key):
        _mounted = os.environ.get(f"LQABR_{_std_key}")
        if _mounted:
            os.environ[_std_key] = _mounted

    prompt = (f"endedReason: {ended_reason or 'unknown'}\n\n"
              f"Transcript:\n{transcript}")

    started = time.perf_counter()
    # No `temperature` on purpose. temperature=0 (for repeatable
    # classifications) was rejected live by the current model: litellm raised
    # UnsupportedParamsError — "claude-sonnet-5 does not support
    # temperature=0. Only temperature=1 is supported" — which knocked every
    # classification down to the deterministic fallback. Determinism here
    # comes from the strict output contract (_parse_model_reply validates the
    # outcome against the enum), not from a sampling knob some models refuse.
    response = litellm.completion(
        model=model,
        messages=[{"role": "system", "content": _SUMMARY_INSTRUCTION},
                  {"role": "user", "content": prompt}],
        max_tokens=500,
    )
    latency_ms = (time.perf_counter() - started) * 1000

    content = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    outcome, summary = _parse_model_reply(content)

    obs.log_model_call(obs.STEP_SUMMARISE, model=model, latency_ms=latency_ms,
                       input_tokens=getattr(usage, "prompt_tokens", None),
                       output_tokens=getattr(usage, "completion_tokens", None),
                       outcome=outcome)

    return {"outcome": outcome, "summary": summary, "classified_by": "model"}


_VALID_OUTCOMES = {o.value for o in VoiceOutcome}


def _parse_model_reply(content: str) -> tuple[str, str]:
    """Pull the outcome and summary out of the model's reply.

    Tolerant of a fenced code block, because models add them despite being
    asked not to. An unparseable or out-of-vocabulary outcome raises rather
    than guessing — a wrong outcome writes a wrong probability into the system
    of record, so `summarise_report`'s explicit fallback path is the right
    place to handle it.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"model reply contained no JSON object: {content[:200]!r}")

    parsed = json.loads(text[start:end + 1])
    outcome = str(parsed.get("outcome", "")).strip().lower()
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(f"model returned unknown outcome {outcome!r}; "
                         f"expected one of {sorted(_VALID_OUTCOMES)}")
    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        raise ValueError("model returned an empty summary")
    return outcome, summary


# ==========================================================================
# STEP 8 — Push It to MCP
# ==========================================================================

def push_to_mcp(contact_id: str, outcome: str, summary: str = "",
                recording_url: str = "", call_id: str = "") -> Dict[str, Any]:
    """STEP 8 — make this call's result the lead's state in HubSpot.

    Runs Rev 5's two writes in order: upsert_lead() sets voice_status for the
    outcome, then record_event() applies the probability increment from
    lqabr_core.probability and logs the engagement event. An engaged call
    records two events, which is what carries a lead from the 30 entry point to
    SCHEDULING_THRESHOLD and closes the loop back to Step 1.

    The summary and recordingUrl travel on the event's `detail`. Rev 5's Data
    Fields Reference lists no HubSpot property for either, and this codebase
    does not invent fields — so they are attached where they are preserved and
    readable rather than written to a property that does not exist.

    A partial write comes back as status "partial" with the failures listed,
    never as a success: "voice_status written, probability not" is a materially
    different state to recover from than "nothing was written".
    """
    with obs.step(obs.STEP_PUSH_MCP, contact_id=contact_id, outcome=outcome) as step_result:
        detail_parts = [p for p in (
            f"call={call_id}" if call_id else "",
            f"recording={recording_url}" if recording_url else "",
            (summary or "").strip(),
        ) if p]
        detail = " | ".join(detail_parts)[:1000]

        try:
            result = mcp.record_call_outcome(contact_id, outcome, detail=detail)
        except CRMError as exc:
            step_result["status"] = "error"
            return {"status": "error", "reason": f"crm-error: {exc}",
                    "contact_id": contact_id, "outcome": outcome}

        result["summary"] = summary
        result["recording_url"] = recording_url
        step_result["status"] = result.get("status", "ok")
        step_result["probability"] = result.get("probability")
        step_result["promoted_to_scheduling"] = result.get("promoted_to_scheduling")
        if result.get("failures"):
            step_result["failures"] = result["failures"]
        return result


# ==========================================================================
# Entrypoints — one per inbound route
# ==========================================================================

def handle_new_lead(object_id: str) -> Dict[str, Any]:
    """Steps 3 → 4 for one newly-eligible lead, in a single invocation.

    `object_id` is HubSpot's own internal contact id (the gateway's
    `objectId`), so it will turn out to be the same value Step 3 reads back as
    `lead.contact_id` once the lookup confirms it — no separate
    external identifier to reconcile anymore. Kept as `object_id` here (rather
    than renamed to `contact_id`) because at this point it's still just the
    raw value the gateway handed us, not yet a lookup-confirmed contact.

    Called by the Step 2 route's in-process handoff. Every exit path returns a
    dict describing what happened; nothing is raised past this boundary,
    because the caller is a background task with no client left to tell.
    """
    read = get_lead(object_id)
    if not read.get("callable"):
        return {"status": "stopped", "step": "3", "object_id": object_id,
                "reason": read.get("reason")}

    lead = VoiceLead(**{k: v for k, v in read["lead"].items()
                        if k in VoiceLead.__dataclass_fields__})

    # Claim the lead BEFORE dialling. Two things depend on this ordering:
    #
    #  1. Deduplication. INITIATED is a blocking voice_status (see
    #     VoiceLead.TERMINAL_VOICE_STATUSES), so a redelivered gateway request
    #     for a lead already in flight stops at Step 3 instead of placing a
    #     second call to a real person. Written after the dial, the guard would
    #     be useless for exactly the window that matters.
    #  2. Write ordering. A call that fails fast (customer-busy, a
    #     call.start.error-*) can produce its end-of-call report — and Step 8's
    #     COMPLETED/FAILED write — within a second. HubSpot has no conditional
    #     update, so an INITIATED write issued after the dial can land last and
    #     overwrite the real outcome, leaving a finished call showing INITIATED
    #     forever.
    #
    # Failing this write must not cancel the call: dedupe protection degrades,
    # but a lead that is otherwise ready to be worked is not dropped.
    try:
        mcp.upsert_lead(lead.contact_id, voice_status="INITIATED")
    except CRMError as exc:
        obs.log_process(obs.STEP_PLACE_CALL, "degraded",
                        "voice_status=INITIATED write failed — dialling anyway, "
                        "duplicate protection is degraded for this lead",
                        level=logging.WARNING, error=str(exc)[:300],
                        contact_id=lead.contact_id)

    with obs.step(obs.STEP_PLACE_CALL, contact_id=lead.contact_id) as step_result:
        try:
            placed = place_call(lead)
        except VapiError as exc:
            step_result["status"] = "error"
            _release_claim(lead.contact_id, f"vapi-error: {exc}")
            return {"status": "error", "step": "4",
                    "contact_id": lead.contact_id,
                    "reason": f"vapi-error: {exc}"}
        except Exception as exc:  # noqa: BLE001
            # Verified live: a SecretNotFoundError out of VapiClient() (missing
            # LQABR_VAPI_API_KEY + broken Secret Manager auth) escaped here,
            # and because it isn't a VapiError the claim was never released —
            # the lead sat INITIATED with no call in flight, and every retry
            # was then refused as a duplicate. No call exists in any path that
            # raises out of place_call, so releasing is always correct.
            step_result["status"] = "error"
            _release_claim(lead.contact_id,
                           f"pre-dial failure: {type(exc).__name__}")
            return {"status": "error", "step": "4",
                    "contact_id": lead.contact_id,
                    "reason": f"pre-dial failure: {type(exc).__name__}: {exc}"}

        if placed.get("error"):
            step_result["status"] = "stopped"
            step_result["reason"] = placed["error"]
            _release_claim(lead.contact_id, placed["error"])
            return {"status": "stopped", "step": "4",
                    "contact_id": lead.contact_id,
                    "reason": placed["error"]}

        step_result["call_id"] = placed.get("call_id")

    return {"status": "initiated", "step": "4",
            "contact_id": lead.contact_id,
            "call_id": placed.get("call_id"), "to": placed.get("to")}


def _release_claim(contact_id: Optional[str], reason: str) -> None:
    """Undo the pre-dial INITIATED claim when the call never actually happened.

    The call was never placed in either case that reaches here: Vapi rejected
    the request outright, or one of place_call's own boundary checks (no
    phone, opted out, no LQABR_VAPI_PHONE_NUMBER_ID) stopped it before Vapi was
    even contacted. Leaving voice_status at INITIATED would strand the lead —
    it is a blocking status (VoiceLead.IN_FLIGHT_VOICE_STATUSES) and there is
    no retry/expiry logic, so it would never become callable again.

    Written as FAILED (2026-08-06, user decision): every never-dialed case
    here — opted-out, no phone on file, no LQABR_VAPI_PHONE_NUMBER_ID
    configured, or Vapi itself rejecting the request — now reports the same
    as a call that was placed and never answered. This deliberately gives up
    the "never dialed" vs "dialed, no answer" distinction on the contact
    record in exchange for not leaving permanently-blocked leads (opted-out,
    bad phone number) sitting at PENDING as if they were still waiting their
    turn.
    """
    if not contact_id:
        return
    try:
        mcp.upsert_lead(contact_id, voice_status="FAILED")
    except CRMError as exc:
        obs.log_process(obs.STEP_PLACE_CALL, "degraded",
                        "call was never placed but the FAILED rollback also "
                        "failed — contact is stranded on INITIATED",
                        level=logging.ERROR, error=str(exc)[:300],
                        contact_id=contact_id, dial_failure_reason=reason)


def handle_call_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Steps 7 → 8 for one end-of-call report, in a single invocation.

    Called by the Step 6→7 route's in-process handoff with Vapi's unwrapped
    `message` object.

    Resolving the contact is the one thing that can fail before Step 7: the
    report carries the call, not the CRM id. `customer.number` is looked up by
    phone, which is the only identifier Vapi guarantees to send back. When the
    lookup fails, Step 7 still runs and its verdict is logged — the outcome of
    a real conversation is worth recording in the logs even when it cannot be
    persisted to the contact.
    """
    ended_reason = str(report.get("endedReason") or "")
    artifact = report.get("artifact") or {}
    transcript = str(artifact.get("transcript") or "")
    recording_url = str(artifact.get("recordingUrl") or "")
    call = report.get("call") or {}
    call_id = str(call.get("id") or "")

    decided = summarise_report(ended_reason=ended_reason, transcript=transcript)

    contact_id = _contact_id_for_report(report)
    if not contact_id:
        obs.log_process(obs.STEP_PUSH_MCP, "stopped",
                        "no HubSpot contact resolved for this call — Step 8 "
                        "cannot write the outcome",
                        level=logging.ERROR, call_id=call_id,
                        outcome=decided["outcome"])
        return {"status": "stopped", "step": "8", "call_id": call_id,
                "outcome": decided["outcome"], "summary": decided["summary"],
                "reason": "not-found: could not resolve a HubSpot contact for "
                          "this call report"}

    # Idempotency (M5). Vapi now retries the end-of-call report on a failed
    # delivery (server.backoffPlan.maxRetries > 0 — see tools.py), so the same
    # report can arrive more than once. Step 8's writes are additive: a second
    # delivery of an engaged call would apply CALL_ANSWERED + CALL_ENGAGED
    # again (30 -> 60 -> 90) and wrongly promote the lead to Scheduling. If this
    # lead's call has already reported a terminal outcome, the write already
    # happened — acknowledge and skip. (A genuinely new call sits at INITIATED,
    # not a terminal status, so this does not block a re-dialed lead's report.
    # Two reports racing before the first COMPLETED write is the residual gap; a
    # call_id dedup store would close it, but there is none yet.)
    try:
        existing = mcp.get_lead(contact_id)
    except Exception as exc:  # noqa: BLE001 — dedup is best-effort: a failed
        # pre-check (CRM error, missing credential, anything) must never block a
        # real call outcome from being written. Fall through to Step 8, whose
        # own writes are guarded and reported.
        existing = None
        obs.log_process(obs.STEP_PUSH_MCP, "degraded",
                        "idempotency pre-check (get_lead) failed — proceeding "
                        "with Step 8 without the duplicate guard",
                        level=logging.WARNING, error=str(exc)[:200],
                        call_id=call_id, contact_id=contact_id)
    if existing is not None and existing.is_complete:
        obs.log_process(obs.STEP_PUSH_MCP, "skipped",
                        "duplicate end-of-call report — voice_status is already "
                        "terminal, the outcome was already written; skipping "
                        "Step 8 to stay idempotent",
                        call_id=call_id, contact_id=contact_id,
                        voice_status=existing.voice_status,
                        outcome=decided["outcome"])
        return {"status": "duplicate", "step": "8", "call_id": call_id,
                "contact_id": contact_id, "outcome": decided["outcome"],
                "summary": decided["summary"],
                "voice_status": existing.voice_status}

    written = push_to_mcp(contact_id, decided["outcome"],
                          summary=decided["summary"],
                          recording_url=recording_url, call_id=call_id)
    return {"status": written.get("status", "ok"), "step": "8",
            "call_id": call_id, "contact_id": contact_id,
            "outcome": decided["outcome"], "summary": decided["summary"],
            "classified_by": decided.get("classified_by"),
            "probability": written.get("probability"),
            "promoted_to_scheduling": written.get("promoted_to_scheduling"),
            "failures": written.get("failures") or []}


def _contact_id_for_report(report: Dict[str, Any]) -> Optional[str]:
    """Resolve the HubSpot contact this report belongs to.

    Preference order is cheapest-and-most-certain first: the id Step 4 put on
    the call itself, then the customer's phone number (a CRM search, and one
    that misses whenever the stored number is formatted differently to the one
    we dialled).

    The id rides in `assistantOverrides.variableValues`, which comes back on
    the Call object inside the report as well as at the top level depending on
    which shape Vapi sends — both are checked rather than assumed.
    """
    call = report.get("call") or {}
    for source in (report.get("assistantOverrides"),
                   call.get("assistantOverrides"),
                   (report.get("assistant") or {}).get("metadata"),
                   report.get("metadata")):
        if not isinstance(source, dict):
            continue
        variables = source.get("variableValues")
        if not isinstance(variables, dict):
            variables = source
        candidate = variables.get("contact_id")
        if candidate:
            return str(candidate)

    customer = report.get("customer") or call.get("customer") or {}
    number = customer.get("number")
    if not number:
        return None
    try:
        lead = mcp.find_lead_by_phone(str(number))
    except CRMError as exc:
        obs.log_process(obs.STEP_PUSH_MCP, "error",
                        "contact lookup by phone failed", level=logging.ERROR,
                        error=str(exc)[:300])
        return None
    return lead.contact_id if lead else None


# ==========================================================================
# The ADK agent
# ==========================================================================

def list_text_voice_queue(limit: int = 25) -> Dict[str, Any]:
    """Leads promoted into this agent's stage (probability >= 30).

    Wrapped in its own error handling because it is the operator-facing tool:
    an unhandled CRMError here takes down the whole adk session rather than
    reporting that HubSpot is unreachable.
    """
    try:
        leads = mcp.leads_in_stage(LeadStage.TEXT_VOICE_OUTREACH, limit=limit)
    except CRMError as exc:
        return {"error": f"crm-error: {exc}", "count": 0, "leads": []}
    except Exception as exc:  # noqa: BLE001 — e.g. SecretNotFoundError before
        # any request is even made. Verified live: an uncaught one here 500s
        # the whole adk session instead of reporting the misconfiguration.
        return {"error": f"config-error: {type(exc).__name__}: {exc}",
                "count": 0, "leads": []}
    return {"count": len(leads),
            "entry_threshold": TEXT_VOICE_THRESHOLD,
            "threshold_to_scheduling": SCHEDULING_THRESHOLD,
            "leads": [lead.to_dict() for lead in leads]}


obs.configure()


# `root_agent` (a custom google.adk.agents.BaseAgent — see adk_agent.py) is
# deliberately NOT built in this file. This module is what the production
# webhook (tools.py's /voice_agent/lead and /voice_agent/vapi_report handlers) actually imports and
# calls into — nothing here should require `google-adk`/`google-genai` to be
# installed just to run the real pipeline. `adk_agent.py` imports the plain
# functions below (get_lead, handle_new_lead, handle_call_report,
# list_text_voice_queue) and wraps them for `adk web`/`adk run` use; this
# file has no idea that wrapper exists.

__all__ = [
    "get_lead", "summarise_report", "push_to_mcp",
    "handle_new_lead", "handle_call_report", "list_text_voice_queue",
]
