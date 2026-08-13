"""The closed Mailgun vocabulary — step 7/8/9's shared contract."""

import pytest

from enums import (
    HUBSPOT_EMAIL_STATUS,
    POSITIVE_EVENTS,
    SCORED_AS,
    TERMINAL_EVENTS,
    MailgunEvent,
    from_mailgun,
    is_terminal,
    resolve_status,
)
from mcp.hubspot.schema import EMAIL_STATUS_VALUES


def test_enum_is_the_eight_values_the_design_closes_on():
    assert {e.value for e in MailgunEvent} == {
        "delivered", "opened", "clicked", "failed",
        "bounced", "complained", "unsubscribed", "stopped",
    }


def test_every_member_is_either_positive_or_terminal():
    assert POSITIVE_EVENTS | TERMINAL_EVENTS == set(MailgunEvent)
    assert not (POSITIVE_EVENTS & TERMINAL_EVENTS)


@pytest.mark.parametrize("wire,severity,expected", [
    ("delivered", "", MailgunEvent.DELIVERED),
    ("opened", "", MailgunEvent.OPENED),
    ("clicked", "", MailgunEvent.CLICKED),
    ("complained", "", MailgunEvent.COMPLAINED),
    ("unsubscribed", "", MailgunEvent.UNSUBSCRIBED),
    ("rejected", "", MailgunEvent.STOPPED),
    ("DELIVERED", "", MailgunEvent.DELIVERED),
])
def test_direct_wire_events_map_onto_the_enum(wire, severity, expected):
    assert from_mailgun(wire, severity) is expected


def test_failed_splits_on_severity_because_mailgun_never_says_bounced():
    assert from_mailgun("failed", "permanent") is MailgunEvent.BOUNCED
    assert from_mailgun("failed", "temporary") is MailgunEvent.FAILED
    assert from_mailgun("failed", "") is MailgunEvent.FAILED


def test_events_outside_the_vocabulary_are_none_not_guessed():
    assert from_mailgun("accepted") is None
    assert from_mailgun("stored") is None
    assert from_mailgun("") is None


def test_terminal_set_matches_is_terminal():
    for event in MailgunEvent:
        assert is_terminal(event) is (event in TERMINAL_EVENTS)


def test_only_engagement_events_are_scored():
    assert set(SCORED_AS) == POSITIVE_EVENTS


def test_every_status_maps_to_a_real_hubspot_enum_value():
    for event in MailgunEvent:
        assert HUBSPOT_EMAIL_STATUS[event] in EMAIL_STATUS_VALUES


def test_clicked_records_as_opened_since_the_enum_has_no_clicked():
    assert HUBSPOT_EMAIL_STATUS[MailgunEvent.CLICKED] == "OPENED"


def test_first_event_always_wins():
    assert resolve_status(None, MailgunEvent.DELIVERED) == (MailgunEvent.DELIVERED, True)


def test_stronger_status_supersedes_weaker():
    assert resolve_status(MailgunEvent.DELIVERED, MailgunEvent.CLICKED) == (
        MailgunEvent.CLICKED, True)


def test_late_weaker_event_does_not_walk_the_record_back():
    # `opened` and `delivered` are asynchronous and can arrive out of order.
    winner, changed = resolve_status(MailgunEvent.CLICKED, MailgunEvent.DELIVERED)
    assert winner is MailgunEvent.CLICKED
    assert changed is False


def test_a_bounce_outranks_any_stale_engagement():
    winner, changed = resolve_status(MailgunEvent.CLICKED, MailgunEvent.BOUNCED)
    assert winner is MailgunEvent.BOUNCED and changed is True
    assert resolve_status(MailgunEvent.BOUNCED, MailgunEvent.OPENED)[1] is False
