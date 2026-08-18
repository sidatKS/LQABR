"""Rev 5 STEPS 3, 7 and 8 — the model and its tools.

    STEP 3  get_lead()          Read the lead from the MCP folder before dialing.
    STEP 4  (delegated)         tools.place_call() — the one outbound leg.
    STEP 7  summarise_report()  The agent reads transcript + endedReason and
                                decides what actually happened on the call.
    STEP 8  push_to_mcp()       Write the outcome back so HubSpot reflects it.

    uvicorn tools:app --port 8082          # from agents/text_voice/src
    ./push_test.sh                          # local end-to-end push test

    (ADK was removed from this agent on 2026-08-18 — no `adk web/run/api_server`.)

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
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote

from lqabr_core import observability as obs
from lqabr_core.crm import hubspot as _hubspot
from lqabr_core.crm.base import CRMError
from lqabr_core.model import ensure_provider_key
from lqabr_core.probability import SCHEDULING_THRESHOLD, TEXT_VOICE_THRESHOLD
from lqabr_core.types import (EngagementEvent, EventType, LeadStage, VoiceLead,
                              VoiceOutcome)

try:
    from .tools import VapiError, place_call
except ImportError:  # pragma: no cover - uvicorn/pytest put src/ on sys.path
    from tools import VapiError, place_call  # type: ignore


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
    # When this agent last wrote a voice status. Real API name, typo included
    # (confirmed from the portal's own property definition). Step 3 uses it to
    # age out a lead stuck in an in-flight state — see STUCK_IN_FLIGHT_MINUTES.
    "last_modfied_voice",
    # SP-2 FR4. A real contact property on portal 246777241 (type string,
    # label "lead_context", description "Lead context notes") — verified
    # 2026-08-17 via the properties API, not read off the UI label. It is
    # deliberately NOT a VoiceLead field: VoiceLead lives in
    # packages/lqabr_core/types.py, and lead_context is a Vapi-call concern
    # rather than part of the lead profile. It travels as its own value from
    # get_lead() -> handle_new_lead() -> place_call().
    "lead_context",
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
                        "VOICEMAIL_LEFT", "CALL_PLACED")

# The in-flight lifecycle (decision, Rao, 2026-08-10). The pre-dial state is
# split in two so "we have claimed this lead" and "Vapi has actually accepted
# the call" are distinguishable:
#
#     PENDING -> INITIATED    (request received, written BEFORE the dial)
#             -> CALL_PLACED  (Vapi accepted and returned a call id)
#             -> COMPLETED | VOICEMAIL_LEFT | FAILED   (end-of-call report)
#
# Both in-flight states block a re-dial. They are defined HERE rather than
# extended onto `VoiceLead.IN_FLIGHT_VOICE_STATUSES`, which lives in
# packages/lqabr_core/types.py — shared code this agent does not own. Step 3
# layers this check on top of `lead.blocking_reason()` instead.
_IN_FLIGHT_VOICE_STATUSES = ("INITIATED", "CALL_PLACED")

# Terminal states an in-flight write must never overwrite. A call that fails
# fast can produce its end-of-call report within a second, so the CALL_PLACED
# write issued just after the dial can otherwise land AFTER the report's
# COMPLETED and leave a finished call showing CALL_PLACED forever. HubSpot has
# no conditional update (no ETag/If-Match), so this is a read-then-write guard,
# not an atomic one — it closes the common case, not the last millisecond.
_TERMINAL_VOICE_STATUSES = ("COMPLETED", "VOICEMAIL_LEFT", "FAILED")

# How long a lead may sit in an in-flight state before a new request is allowed
# to re-dial it. Without this a lost end-of-call report (a crashed revision, a
# webhook that never arrives) strands the lead forever: INITIATED is blocking
# and nothing ever clears it. Measured against `last_modfied_voice`, which this
# agent stamps on every write it makes.
STUCK_IN_FLIGHT_MINUTES = int(
    os.environ.get("LQABR_STUCK_IN_FLIGHT_MINUTES", "15"))

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


def _epoch_ms(raw: Any) -> Optional[int]:
    """Parse `last_modfied_voice` into epoch milliseconds, or None.

    Two shapes on purpose. This agent WRITES the property as an epoch-ms
    string (`str(int(time.time() * 1000))`), but HubSpot types it as a
    datetime and READS it back as ISO 8601 — a real GET on contact
    533963448041 returned `"2026-08-17T14:44:32.633Z"`, not digits. Handling
    only one of the two would silently return None and disable the stuck-lead
    age window, which fails open (the lead is never re-dialled) rather than
    loudly.

    Anything unparseable is None, and callers treat None as "no timestamp, do
    not age this lead out" — the conservative direction.
    """
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if text.isdigit():
        return int(text)
    try:
        # HubSpot's trailing Z is not accepted by fromisoformat before 3.11.
        return int(datetime.fromisoformat(
            text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


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
        lead, _ = self.get_lead_with_extras(object_id)
        return lead

    def get_lead_with_extras(
            self, object_id: str) -> tuple[Optional[VoiceLead], Dict[str, Any]]:
        """The lead plus the contact properties `VoiceLead` cannot carry.

        Returns `(lead, extras)` where extras holds:
            lead_context           str  — SP-2 FR4's per-lead opening context
            voice_status_written_ms int|None — `last_modfied_voice` as epoch ms,
                                    used to age out a stuck in-flight lead

        Both come from the SAME GET Step 3 already makes — no second CRM call.
        They live in a dict rather than on `VoiceLead` because that dataclass
        is in packages/lqabr_core, shared code this agent does not own; adding
        fields there would change a type the email, scheduling and orchestrator
        agents all import.
        """
        contact = self._contact_by_id(object_id)
        if contact is None:
            return None, {"lead_context": "", "voice_status_written_ms": None}
        properties = contact.get("properties") or {}
        lead = self._to_voice_lead(contact, self._primary_company(contact))
        return lead, {
            "lead_context": str(properties.get("lead_context") or ""),
            "voice_status_written_ms": _epoch_ms(
                properties.get("last_modfied_voice")),
        }

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

def _blocking_reason(lead: VoiceLead, status_written_ms: Optional[int],
                     outcome: Dict[str, Any]) -> Optional[str]:
    """Step 3's stop conditions, extending `VoiceLead.blocking_reason()`.

    Two things this adds, both from the 2026-08-10 state-machine decision, and
    both done HERE because `VoiceLead` lives in packages/lqabr_core:

    1. **CALL_PLACED also blocks.** The base method only knows about
       INITIATED, so without this a duplicate gateway request arriving after
       Vapi accepted the call would dial the same person twice.
    2. **An age window on the in-flight states.** A lead whose end-of-call
       report never arrives is otherwise stranded forever — the status is
       blocking and nothing clears it. After STUCK_IN_FLIGHT_MINUTES the lead
       becomes dialable again.

    The age window deliberately fails CLOSED when there is no usable
    timestamp: no `last_modfied_voice` means the lead stays blocked rather
    than being re-dialled on a guess. A real person's phone is on the other
    end of guessing wrong.
    """
    status = (lead.voice_status or "").upper()

    if status in _IN_FLIGHT_VOICE_STATUSES:
        if status_written_ms is None:
            return (f"in-flight: voice_status={lead.voice_status} "
                    "(no last_modfied_voice — cannot age it out)")
        age_minutes = (time.time() * 1000 - status_written_ms) / 60000
        if age_minutes < STUCK_IN_FLIGHT_MINUTES:
            return (f"in-flight: voice_status={lead.voice_status} "
                    f"({age_minutes:.1f} min old, window is "
                    f"{STUCK_IN_FLIGHT_MINUTES} min)")
        obs.log_process(obs.STEP_READ_LEAD, "degraded",
                        "in-flight claim is stale — its end-of-call report "
                        "never arrived, so this lead is being released for a "
                        "re-dial",
                        level=logging.WARNING, contact_id=lead.contact_id,
                        voice_status=lead.voice_status,
                        age_minutes=round(age_minutes, 1))
        outcome["released_stale_claim"] = True
        # Fall through to the base checks: a stale claim releases the in-flight
        # block, it does not excuse a missing phone number.

    base = lead.blocking_reason()
    # The base method blocks INITIATED unconditionally; that decision now
    # belongs to the branch above, so drop its verdict for the in-flight case
    # only. Every other reason it gives (no phone, opted out, already
    # complete) still stands.
    if base and base.startswith("in-flight:"):
        return None
    return base


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
            # `get_lead_with_context` returns FR4's lead_context from the same
            # GET. Guarded with getattr because `mcp` may resolve to the real
            # Step 5 tool module (see _resolve_mcp), which only promises the
            # five names in _MCP_TOOL_NAMES — a lead is still callable without
            # context, it just opens on the generic intro.
            reader = getattr(mcp, "get_lead_with_extras", None)
            if reader is not None:
                lead, extras = reader(object_id)
            else:
                lead, extras = mcp.get_lead(object_id), {}
            lead_context = str(extras.get("lead_context") or "")
            status_written_ms = extras.get("voice_status_written_ms")
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

        reason = _blocking_reason(lead, status_written_ms, outcome)

        if reason:
            outcome["status"] = "stopped"
            outcome["reason"] = reason
            return {"callable": False, "reason": reason, "lead": lead.to_dict(),
                    "contact_id": lead.contact_id}

        outcome["probability"] = lead.probability
        # The raw property value as read from HubSpot, on process_log (user
        # request 2026-08-17). Step 4 logs the capped value it actually sent —
        # comparing the two is how you tell a truncation problem from a
        # wrong-content problem. `lead_context_chars` of 0 is the signal that
        # this call will open on the generic intro rather than on FR4's context.
        outcome["lead_context"] = lead_context
        outcome["lead_context_chars"] = len(lead_context)
        return {"callable": True, "lead": lead.to_dict(),
                "contact_id": lead.contact_id,
                "lead_context": lead_context}


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

    # Provider keys now go through Secret Manager on this path too (2026-08-07,
    # user request) — supersedes the 2026-07-31 "ENV-ONLY, same policy as the
    # Vapi key" decision. litellm reads each provider's OWN standard env var
    # (ANTHROPIC_API_KEY for Anthropic), while ops mounts/stores keys under the
    # lqabr-* convention (LQABR_ANTHROPIC_API_KEY / Secret Manager's
    # lqabr-anthropic-api-key). ensure_provider_key() is the single place that
    # knows that mapping (shared with model.py's build_model()) — env var
    # wins if already set, else it checks
    # LQABR_ANTHROPIC_API_KEY, else it fetches from Secret Manager.
    ensure_provider_key(model)

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

    lead_context = read.get("lead_context") or ""

    with obs.step(obs.STEP_PLACE_CALL, contact_id=lead.contact_id) as step_result:
        try:
            placed = place_call(lead, lead_context=lead_context)
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
        # What Vapi actually received, not what HubSpot held — see place_call.
        step_result["lead_context"] = placed.get("lead_context")
        step_result["lead_context_chars"] = placed.get("lead_context_chars")
        step_result["voice_status"] = _mark_call_placed(lead.contact_id)

    return {"status": "initiated", "step": "4",
            "contact_id": lead.contact_id,
            "call_id": placed.get("call_id"), "to": placed.get("to"),
            "lead_context_chars": placed.get("lead_context_chars")}


def _mark_call_placed(contact_id: Optional[str]) -> str:
    """Advance INITIATED -> CALL_PLACED now that Vapi has accepted the call.

    The no-downgrade guard (2026-08-10 decision) lives here rather than in the
    CRM layer, and it guards THIS write specifically. INITIATED is always the
    first write of a call's life, so it can never land out of order; a retry
    writing INITIATED over a previous call's FAILED is a legitimate re-claim
    and must be allowed. CALL_PLACED is the only in-flight write that races a
    terminal one — a call that fails fast (customer-busy, a call.start.error-*)
    can have its end-of-call report, and Step 8's COMPLETED/FAILED write,
    land within a second of the dial.

    HubSpot has no conditional update — no ETag, no If-Match — so this is
    read-then-write. It closes the common case; two writes inside the same
    few milliseconds can still interleave. Recording that residual race here
    rather than pretending it is solved.

    Never raises: the call is already in flight, and losing the CALL_PLACED
    marker must not look like a failed dial. Returns the status it settled on,
    for the step log.
    """
    if not contact_id:
        return "unknown"
    try:
        current = mcp.get_lead(contact_id)
    except Exception as exc:  # noqa: BLE001 — best-effort guard; a failed
        # pre-check must not stop the marker being written.
        current = None
        obs.log_process(obs.STEP_PLACE_CALL, "degraded",
                        "could not re-read voice_status before the CALL_PLACED "
                        "write — writing without the no-downgrade guard",
                        level=logging.WARNING, error=str(exc)[:200],
                        contact_id=contact_id)

    if current is not None and (current.voice_status or "").upper() in _TERMINAL_VOICE_STATUSES:
        obs.log_process(obs.STEP_PLACE_CALL, "skipped",
                        "end-of-call report already wrote a terminal status — "
                        "skipping the CALL_PLACED write so it cannot overwrite "
                        "the real outcome",
                        contact_id=contact_id,
                        voice_status=current.voice_status)
        return current.voice_status or "unknown"

    try:
        mcp.upsert_lead(contact_id, voice_status="CALL_PLACED")
        return "CALL_PLACED"
    except CRMError as exc:
        obs.log_process(obs.STEP_PLACE_CALL, "degraded",
                        "call was placed but the CALL_PLACED write failed — "
                        "the lead stays on INITIATED and will age out of the "
                        "in-flight window normally",
                        level=logging.WARNING, error=str(exc)[:300],
                        contact_id=contact_id)
        return "INITIATED"


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
    # `find_lead_by_phone` returns a **LeadProfile**, not the VoiceLead the
    # rest of this file works with, and LeadProfile's id field is `object_id`
    # — it has no `contact_id` at all (dataclasses.fields(LeadProfile),
    # verified 2026-08-17). Reading `.contact_id` here raised AttributeError,
    # which is not a CRMError, so it escaped this function's own guard and was
    # only ever caught by the catch-all in tools._handoff_call_report — Step 8
    # never ran and the call's outcome was silently lost. Latent until now
    # because Step 4 always puts `contact_id` in variableValues, which the
    # loop above resolves first; this is the fallback for when it doesn't.
    return lead.object_id if lead else None


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


# There is no `root_agent` here, and none anywhere in this agent. ADK was
# removed on 2026-08-18: `adk_agent.py` (the custom BaseAgent wrapper) and
# `agent.py` (the discovery shim) are retired, so `adk web`/`adk run` no longer
# apply to this directory. This module is what the production service
# (tools.py's /voice_agent/lead and /voice_agent/vapi_report handlers) imports
# and calls into, and it has never required `google-adk`/`google-genai` to run
# the real pipeline. The plain functions below (get_lead, handle_new_lead,
# handle_call_report, list_text_voice_queue) are now called only from tools.py.

__all__ = [
    "get_lead", "summarise_report", "push_to_mcp",
    "handle_new_lead", "handle_call_report", "list_text_voice_queue",
]
