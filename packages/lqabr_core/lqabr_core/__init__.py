"""lqabr_core — shared library for all LQABR agents.

Import as `from lqabr_core import ...`. Nothing here talks to an LLM;
this package is the deterministic, typed, mockable layer under every agent:

- types:        LeadProfile (the 9 pointers), LeadStage, LeadSource, EngagementEvent
- probability:  the single source of truth for probability increments/thresholds
- secrets:      Google Secret Manager accessor with env-var fallback for local dev
- crm:          CRMClient interface + HubSpotClient (HubSpot is the system of record)
- timezones:    EST/CST/PST/IST helpers for the Scheduling Agent
"""

from lqabr_core.types import (  # noqa: F401
    EngagementEvent,
    EventType,
    LeadProfile,
    LeadSource,
    LeadStage,
)
from lqabr_core.probability import (  # noqa: F401
    EVENT_INCREMENTS,
    MEETING_SCHEDULED_PROBABILITY,
    SCHEDULING_THRESHOLD,
    TEXT_VOICE_THRESHOLD,
    apply_event,
)

__all__ = [
    "LeadProfile",
    "LeadStage",
    "LeadSource",
    "EngagementEvent",
    "EventType",
    "EVENT_INCREMENTS",
    "TEXT_VOICE_THRESHOLD",
    "SCHEDULING_THRESHOLD",
    "MEETING_SCHEDULED_PROBABILITY",
    "apply_event",
]
