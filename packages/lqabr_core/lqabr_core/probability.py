"""Lead-probability engine — the single source of truth for how engagement
events move a lead's probability (0–100) and which thresholds promote a lead
to the next agent.

Every agent imports these constants; none redefines them. Changing the
scoring model is an edit HERE (or, later, a config override) — never in an
agent.

Flow (see docs/EPICS.md):
    profiled lead starts at BASE_PROBABILITY
    Email Agent events    -> probability rises -> >= TEXT_VOICE_THRESHOLD
    Text/Voice events     -> probability rises -> >= SCHEDULING_THRESHOLD
    Scheduling Agent      -> meeting booked    -> MEETING_SCHEDULED_PROBABILITY
"""

from __future__ import annotations

from typing import Dict, Tuple

from lqabr_core.types import EventType, LeadStage

# Probability a lead gets the moment its 9-pointer profile lands in HubSpot.
BASE_PROBABILITY = 10

# Per-event increments.
EVENT_INCREMENTS: Dict[EventType, int] = {
    EventType.EMAIL_DELIVERED: 2,
    EventType.EMAIL_OPENED: 5,
    EventType.EMAIL_CLICKED: 10,
    EventType.SMS_DELIVERED: 3,
    EventType.VOICEMAIL_LEFT: 10,  # raised from 2 (2026-08-06, user request)
    EventType.CALL_ANSWERED: 15,
    EventType.CALL_ENGAGED: 15,   # on top of CALL_ANSWERED — an answered call
                                  # that completes the Q&A flow reaches the
                                  # scheduling threshold from the text/voice
                                  # entry point (30 + 15 + 15 = 60)
    EventType.CALL_NOT_ANSWERED: 0,  # explicit, not just the .get() default:
                                      # a call attempt that never connected
                                      # must never move probability — it's
                                      # recorded for visibility only.
}

# A booked meeting pins probability here (not incremental).
MEETING_SCHEDULED_PROBABILITY = 95

# Promotion thresholds between agents.
TEXT_VOICE_THRESHOLD = 30    # Email Agent -> Text/Voice Agent
SCHEDULING_THRESHOLD = 60    # Text/Voice Agent -> Scheduling Agent

MAX_PROBABILITY = 100

# HubSpot counter property each event increments (see infra/gcp/04_hubspot_properties.py).
EVENT_COUNTERS: Dict[EventType, str] = {
    EventType.EMAIL_DELIVERED: "lqabr_email_delivered_count",
    EventType.EMAIL_OPENED: "lqabr_email_opened_count",
    EventType.EMAIL_CLICKED: "lqabr_email_clicked_count",
    EventType.SMS_DELIVERED: "lqabr_sms_delivered_count",
    EventType.VOICEMAIL_LEFT: "lqabr_voicemail_count",
    EventType.CALL_ANSWERED: "lqabr_call_answered_count",
    EventType.CALL_ENGAGED: "lqabr_call_engaged_count",
    EventType.CALL_NOT_ANSWERED: "lqabr_call_not_answered_count",
    EventType.MEETING_SCHEDULED: "lqabr_meeting_count",
}


def apply_event(current_probability: int, event_type: EventType) -> int:
    """Return the new probability after one engagement event (capped 0–100)."""
    if event_type is EventType.MEETING_SCHEDULED:
        return max(current_probability, MEETING_SCHEDULED_PROBABILITY)
    increment = EVENT_INCREMENTS.get(event_type, 0)
    return max(0, min(MAX_PROBABILITY, current_probability + increment))


def stage_for_probability(probability: int, meeting_scheduled: bool = False) -> LeadStage:
    """Which agent should own a lead at this probability level."""
    if meeting_scheduled:
        return LeadStage.MEETING_SCHEDULED
    if probability >= SCHEDULING_THRESHOLD:
        return LeadStage.SCHEDULING
    if probability >= TEXT_VOICE_THRESHOLD:
        return LeadStage.TEXT_VOICE_OUTREACH
    return LeadStage.EMAIL_OUTREACH


def promotion(current_probability: int, new_probability: int) -> Tuple[bool, LeadStage]:
    """Did this change cross a threshold, and what stage should the lead be in now?"""
    new_stage = stage_for_probability(new_probability)
    old_stage = stage_for_probability(current_probability)
    return (new_stage != old_stage, new_stage)
"""Lead-probability engine — the single source of truth for how engagement
events move a lead's probability (0–100) and which thresholds promote a lead
to the next agent.

Every agent imports these constants; none redefines them. Changing the
scoring model is an edit HERE (or, later, a config override) — never in an
agent.

Flow (see docs/EPICS.md):
    profiled lead starts at BASE_PROBABILITY
    Email Agent events    -> probability rises -> >= TEXT_VOICE_THRESHOLD
    Text/Voice events     -> probability rises -> >= SCHEDULING_THRESHOLD
    Scheduling Agent      -> meeting booked    -> MEETING_SCHEDULED_PROBABILITY
"""

from __future__ import annotations

from typing import Dict, Tuple

from lqabr_core.types import EventType, LeadStage

# Probability a lead gets the moment its 9-pointer profile lands in HubSpot.
BASE_PROBABILITY = 10

# Per-event increments.
EVENT_INCREMENTS: Dict[EventType, int] = {
    EventType.EMAIL_DELIVERED: 2,
    EventType.EMAIL_OPENED: 5,
    EventType.EMAIL_CLICKED: 10,
    EventType.SMS_DELIVERED: 3,
    EventType.VOICEMAIL_LEFT: 2,
    EventType.CALL_ANSWERED: 15,
    EventType.CALL_ENGAGED: 15,   # on top of CALL_ANSWERED — an answered call
                                  # that completes the Q&A flow reaches the
                                  # scheduling threshold from the text/voice
                                  # entry point (30 + 15 + 15 = 60)
}

# A booked meeting pins probability here (not incremental).
MEETING_SCHEDULED_PROBABILITY = 95

# Promotion thresholds between agents.
TEXT_VOICE_THRESHOLD = 30    # Email Agent -> Text/Voice Agent
SCHEDULING_THRESHOLD = 60    # Text/Voice Agent -> Scheduling Agent

MAX_PROBABILITY = 100

# HubSpot counter property each event increments, if one exists.
#
# SCHEMA NOTE (confirmed live 2026-07-23): the real HubSpot account has
# NO counter properties at all — no email_sent/email_opened/
# call_started/call_completed, no per-event counts of any kind. Email
# engagement is tracked purely via the single current-value
# `lqabr_email_status` field (see hubspot.py), and there is nothing
# equivalent yet for SMS/voicemail/calls/meetings. EVENT_COUNTERS is left
# empty until/unless a real counter property is added to the account —
# HubSpotClient.record_event() treats a missing entry as "nothing to
# increment" rather than raising, so this stays a safe no-op.
EVENT_COUNTERS: Dict[EventType, str] = {}


def apply_event(current_probability: int, event_type: EventType) -> int:
    """Return the new probability after one engagement event (capped 0–100)."""
    if event_type is EventType.MEETING_SCHEDULED:
        return max(current_probability, MEETING_SCHEDULED_PROBABILITY)
    increment = EVENT_INCREMENTS.get(event_type, 0)
    return max(0, min(MAX_PROBABILITY, current_probability + increment))


def stage_for_probability(probability: int, meeting_scheduled: bool = False) -> LeadStage:
    """Which agent should own a lead at this probability level."""
    if meeting_scheduled:
        return LeadStage.MEETING_SCHEDULED
    if probability >= SCHEDULING_THRESHOLD:
        return LeadStage.SCHEDULING
    if probability >= TEXT_VOICE_THRESHOLD:
        return LeadStage.TEXT_VOICE_OUTREACH
    return LeadStage.EMAIL_OUTREACH


def promotion(current_probability: int, new_probability: int) -> Tuple[bool, LeadStage]:
    """Did this change cross a threshold, and what stage should the lead be in now?"""
    new_stage = stage_for_probability(new_probability)
    old_stage = stage_for_probability(current_probability)
    return (new_stage != old_stage, new_stage)
