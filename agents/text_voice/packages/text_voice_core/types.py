"""The voice agent's own domain types.

Carved out of lqabr_core.types (2026-09-08) so this agent imports nothing
from the rest of the repo. Only what text_voice actually uses came across:
LeadProfile, LeadSource and EngagementEvent stayed behind. LeadStage is here
because probability.stage_for_probability() returns one.

CRMError lives here too — it was lqabr_core.crm.base.CRMError, whose other
export (the CRMClient ABC) this agent never used.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional


class CRMError(RuntimeError):
    """A CRM/MCP operation failed after retries. Never swallowed into a success."""


class LeadStage(str, Enum):
    """Pipeline stages a lead moves through. Stored in HubSpot `lqabr_stage`."""

    INGESTED = "ingested"                  # raw record landed
    PROFILED = "profiled"                  # 9-pointer profile built, in HubSpot
    EMAIL_OUTREACH = "email_outreach"      # Email Agent working the lead
    TEXT_VOICE_OUTREACH = "text_voice_outreach"  # Text/Voice Agent working the lead
    SCHEDULING = "scheduling"              # Scheduling Agent working the lead
    MEETING_SCHEDULED = "meeting_scheduled"
    UNRESOLVED = "unresolved"              # bad-data: flagged, never dropped


class EventType(str, Enum):
    """Engagement events that adjust lead probability. See probability.py."""

    EMAIL_DELIVERED = "email_delivered"
    EMAIL_OPENED = "email_opened"
    EMAIL_CLICKED = "email_clicked"
    SMS_DELIVERED = "sms_delivered"
    VOICEMAIL_LEFT = "voicemail_left"
    CALL_ANSWERED = "call_answered"
    CALL_ENGAGED = "call_engaged"          # answered AND completed the Q&A flow
    CALL_NOT_ANSWERED = "call_not_answered"  # placed but never connected: no
                                              # answer, busy, failed, or
                                              # canceled -- from the call's
                                              # terminal StatusCallback, not
                                              # AnsweredBy/AMD (see
                                              # webhook_app.py /voice/status)
    MEETING_SCHEDULED = "meeting_scheduled"


class VoiceOutcome(str, Enum):
    """The four outcomes Step 7 classifies a finished call into.

    Rev 5 Step 6 resolves every call to one of three telephony outcomes (not
    answered / rings-or-busy / answered); Step 7 splits "answered" by whether
    the lead actually engaged, because only engagement earns the second
    probability increment that reaches SCHEDULING_THRESHOLD.
    """

    NOT_ANSWERED = "not_answered"                    # never connected
    VOICEMAIL = "voicemail"                          # machine picked up
    ANSWERED_NOT_ENGAGED = "answered_not_engaged"    # human, but no interest
    ANSWERED_AND_ENGAGED = "answered_and_engaged"    # human, completed the Q&A


@dataclass
class VoiceLead:
    """One lead as Step 3 returns it — Contact fields plus its Company's.

    Field names here are the *spec's* names. The HubSpot API names they map to
    are not the same, and several labels shown in the HubSpot UI differ from
    the real property names again (see lqabr_core.crm.hubspot). Keeping the
    spec's vocabulary here and the API's vocabulary in the adapter is what
    stops the two from being confused at the call site.
    """

    # --- Contact ---------------------------------------------------------
    employee_id: Optional[str] = None
    # Standard HubSpot `email` property (decided 2026-08-26; the custom
    # `email_id` field name is retired repo-wide).
    email: Optional[str] = None
    phone_number: Optional[str] = None
    job_title: Optional[str] = None
    decision_maker: Optional[str] = None
    email_status: Optional[str] = None
    voice_status: Optional[str] = None
    probability: int = 0

    # --- associated Company ----------------------------------------------
    company_id: Optional[str] = None
    industry: Optional[str] = None
    annual_revenue: Optional[str] = None
    frequency_of_purchase: Optional[str] = None

    # --- plumbing (not spec fields, but needed to write back) -------------
    # Renamed hubspot_contact_id -> contact_id (2026-08-06), then contact_id
    # -> object_id (2026-08-24) to match LeadProfile's vocabulary and
    # HubSpot's own webhook payloads (`objectId`). `hubspot_company_id` is
    # left as-is: `company_id` above is already a distinct field (the
    # business `company_id` property on the associated Company), so renaming
    # `hubspot_company_id` -> `company_id` would collide with it.
    object_id: Optional[str] = None
    hubspot_company_id: Optional[str] = None
    full_name: Optional[str] = None
    company_name: Optional[str] = None
    opted_out: bool = False

    # voice_status values meaning this lead's call is already done and must not
    # be re-dialed. Rev 5 Step 3: "confirm this lead hasn't already been
    # completed or opted out". Both are real values of the voice_status
    # enumeration in the portal.
    TERMINAL_VOICE_STATUSES = ("COMPLETED", "VOICEMAIL_LEFT")

    # A call this lead has already been claimed for, which has not reported back
    # yet. Blocking for the same reason the terminal statuses are, but for a
    # different situation, so it is a separate tuple and a separate `reason:`
    # prefix — "we already spoke to them" and "a call is ringing right now" need
    # different responses from whoever reads the log.
    #
    # Written by handle_new_lead BEFORE Step 4 dials, so a redelivered gateway
    # request stops here instead of placing a second call to a real person.
    #
    # Consequence, deliberately accepted: a call whose end-of-call report never
    # arrives (a lost webhook, a crashed revision) leaves the lead stuck on
    # INITIATED and unreachable until someone resets voice_status by hand. That
    # is the chosen side to err on — under-calling beats double-calling — and
    # there is no retry/expiry logic yet.
    IN_FLIGHT_VOICE_STATUSES = ("INITIATED",)

    @property
    def is_complete(self) -> bool:
        return (self.voice_status or "").upper() in self.TERMINAL_VOICE_STATUSES

    @property
    def is_in_flight(self) -> bool:
        return (self.voice_status or "").upper() in self.IN_FLIGHT_VOICE_STATUSES

    @property
    def is_callable(self) -> bool:
        """Every precondition Step 3 must satisfy before Step 4 may dial."""
        return (bool(self.phone_number) and not self.opted_out
                and not self.is_complete and not self.is_in_flight)

    def blocking_reason(self) -> Optional[str]:
        """Why this lead must not be dialed, in the repo's `reason:` format.

        None when the lead is callable. Blocked leads are always flagged with
        an explicit reason and never silently dropped (CLAUDE.md conventions).
        """
        if not self.phone_number:
            return "bad-data: contact has no phone number"
        if self.opted_out:
            return "opted-out: contact has opted out of outreach"
        if self.is_complete:
            return f"already-complete: voice_status={self.voice_status}"
        if self.is_in_flight:
            return f"in-flight: voice_status={self.voice_status}"
        return None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def personalization(self) -> Dict[str, str]:
        """The subset Step 4 sends to Vapi as template variables.

        Rev 5 Step 4 personalizes on Job Title, decision_maker and Industry;
        first name and company name are what make the script speakable. Every
        value is a plain string with a sane fallback — a literal "None" spoken
        down the phone is worse than a generic greeting.
        """
        first_name = (self.full_name or "there").split()[0]
        return {
            "first_name": first_name,
            "full_name": self.full_name or "there",
            "company": self.company_name or "your team",
            "industry": self.industry or "your industry",
            "job_title": self.job_title or "your role",
            "decision_maker": str(self.decision_maker or "unknown"),
            "annual_revenue": str(self.annual_revenue or "unknown"),
            "frequency_of_purchase": str(self.frequency_of_purchase or "unknown"),
        }
