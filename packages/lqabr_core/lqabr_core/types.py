"""Shared LQABR types.

The LeadProfile "9 pointers" are the contract between every agent and HubSpot:

    1. full_name
    2. job_title
    3. company
    4. email
    5. phone
    6. industry
    7. company_size_revenue   (employee count and/or annual revenue)
    8. location  (+ timezone)
    9. linkedin_url

HubSpot is the system of record — a LeadProfile in memory is always a
projection of (or a pending write to) a HubSpot contact, never canonical
state of its own.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class LeadSource(str, Enum):
    """How the lead entered the pipeline (the ingestion trigger's source arg)."""

    CSV = "csv"            # manual: operator drops CSVs in a folder and triggers
    ZOOMINFO = "zoominfo"  # automatic: ZoomInfo API pull (default batch of 20)


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
    MEETING_SCHEDULED = "meeting_scheduled"


@dataclass
class LeadProfile:
    """One lead, shaped as the 9 pointers plus pipeline metadata."""

    # --- the 9 pointers -------------------------------------------------
    full_name: Optional[str] = None
    job_title: Optional[str] = None
    company: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    industry: Optional[str] = None
    company_size_revenue: Optional[str] = None
    location: Optional[str] = None          # includes/implies timezone
    linkedin_url: Optional[str] = None

    # --- pipeline metadata ----------------------------------------------
    timezone: Optional[str] = None          # IANA tz derived from location
    source: LeadSource = LeadSource.CSV
    external_employee_id: Optional[str] = None   # seed/ZoomInfo person id
    external_company_id: Optional[str] = None
    stage: LeadStage = LeadStage.INGESTED
    probability: int = 0
    hubspot_contact_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    POINTER_FIELDS = (
        "full_name",
        "job_title",
        "company",
        "email",
        "phone",
        "industry",
        "company_size_revenue",
        "location",
        "linkedin_url",
    )

    def pointers(self) -> Dict[str, Optional[str]]:
        """Just the 9-pointer view of the profile."""
        return {name: getattr(self, name) for name in self.POINTER_FIELDS}

    def missing_pointers(self) -> list[str]:
        return [name for name in self.POINTER_FIELDS if not getattr(self, name)]

    @property
    def is_contactable(self) -> bool:
        """A lead must have at least one channel (email or phone) to be worked."""
        return bool(self.email or self.phone)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["source"] = self.source.value
        data["stage"] = self.stage.value
        return data


@dataclass(frozen=True)
class EngagementEvent:
    """A single engagement signal for a lead, e.g. from a Mailgun/Twilio webhook."""

    event_type: EventType
    hubspot_contact_id: str
    occurred_at: Optional[str] = None   # ISO-8601
    detail: Optional[str] = None        # e.g. clicked URL, call SID, message id
