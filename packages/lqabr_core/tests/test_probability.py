from lqabr_core.probability import (
    BASE_PROBABILITY,
    EVENT_COUNTERS,
    EVENT_INCREMENTS,
    MEETING_SCHEDULED_PROBABILITY,
    SCHEDULING_THRESHOLD,
    TEXT_VOICE_THRESHOLD,
    apply_event,
    promotion,
    stage_for_probability,
)
from lqabr_core.types import EventType, LeadStage


def test_every_event_type_has_a_counter():
    assert set(EVENT_COUNTERS) == set(EventType)


def test_increments_apply_and_cap():
    assert apply_event(0, EventType.EMAIL_DELIVERED) == 2
    assert apply_event(10, EventType.EMAIL_OPENED) == 15
    assert apply_event(10, EventType.EMAIL_CLICKED) == 20
    assert apply_event(99, EventType.CALL_ANSWERED) == 100  # capped


def test_meeting_scheduled_pins_probability():
    assert apply_event(12, EventType.MEETING_SCHEDULED) == MEETING_SCHEDULED_PROBABILITY
    assert apply_event(99, EventType.MEETING_SCHEDULED) == 99  # never lowers


def test_thresholds_order():
    assert BASE_PROBABILITY < TEXT_VOICE_THRESHOLD < SCHEDULING_THRESHOLD < MEETING_SCHEDULED_PROBABILITY


def test_stage_for_probability():
    assert stage_for_probability(BASE_PROBABILITY) is LeadStage.EMAIL_OUTREACH
    assert stage_for_probability(TEXT_VOICE_THRESHOLD) is LeadStage.TEXT_VOICE_OUTREACH
    assert stage_for_probability(SCHEDULING_THRESHOLD) is LeadStage.SCHEDULING
    assert stage_for_probability(50, meeting_scheduled=True) is LeadStage.MEETING_SCHEDULED


def test_promotion_detects_threshold_crossing():
    promoted, stage = promotion(TEXT_VOICE_THRESHOLD - 2, TEXT_VOICE_THRESHOLD + 3)
    assert promoted and stage is LeadStage.TEXT_VOICE_OUTREACH
    promoted, _ = promotion(12, 14)
    assert not promoted


def test_call_answered_plus_engaged_reaches_scheduling_from_threshold():
    p = TEXT_VOICE_THRESHOLD + EVENT_INCREMENTS[EventType.SMS_DELIVERED]
    p = apply_event(p, EventType.CALL_ANSWERED)
    p = apply_event(p, EventType.CALL_ENGAGED)
    assert p >= SCHEDULING_THRESHOLD
