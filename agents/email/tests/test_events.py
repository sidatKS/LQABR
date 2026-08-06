"""Business logic 2 — steps 8 and 9."""

import pytest

import events
from email_fakes import FakeCRM, FakeSession
from lqabr_core.crm import CRMError
from mcp.hubspot.schema import ValidatedProfile
from runstate import LeadRunRecord

TOKEN = "trg-1:run-1"


def profile(object_id="42", probability=10):
    return ValidatedProfile(object_id=object_id, email_id="jane@acme.example",
                            employee_id="E00002", company="Acme", probability=probability)


def event_data(name="delivered", token=TOKEN, object_id="42", severity="",
               message_id="<m-1@mg>"):
    variables = {}
    if token:
        variables["lqabr_correlation_token"] = token
    if object_id:
        variables["lqabr_object_id"] = object_id
    return {"event": name, "severity": severity, "user-variables": variables,
            "message": {"headers": {"message-id": message_id}}}


@pytest.fixture
def sent(store):
    """A run that has already sent one email — the step-7 output step 8 needs."""
    store.record_send("trg-1", "run-1", LeadRunRecord(
        object_id="42", email="jane@acme.example", message_id="<m-1@mg>", skill="technology"))
    return store


def session_with(probability=10):
    return FakeSession(FakeCRM(profiles={"42": profile(probability=probability)}))


# ------------------------------------------------------------------ step 8
def test_delivered_is_matched_by_token_and_written_back(sent):
    session = session_with(probability=10)
    result = events.handle_event(event_data("delivered"), session=session, store=sent)

    assert result["status"] == "recorded"
    assert result["email_status"] == "DELIVERED"
    assert result["probability"] == 12          # +2, from lqabr_core.probability
    assert session.crm.patches[0][1]["lqabr_email_status"] == "DELIVERED"


def test_opened_and_clicked_use_the_shared_increments(sent):
    session = session_with(probability=12)
    opened = events.handle_event(event_data("opened"), session=session, store=sent)
    assert opened["probability"] == 17          # +5

    session = session_with(probability=17)
    clicked = events.handle_event(event_data("clicked"), session=session, store=sent)
    assert clicked["probability"] == 27         # +10


def test_an_event_outside_the_vocabulary_is_acknowledged_not_scored(sent):
    session = session_with()
    result = events.handle_event(event_data("accepted"), session=session, store=sent)
    assert result["status"] == "ignored"
    assert session.crm.patches == []


def test_an_event_whose_token_matches_no_run_state_is_flagged_never_guessed(store):
    session = session_with()
    result = events.handle_event(event_data("opened", token="trg-x:run-x"),
                                 session=session, store=store)
    assert result["status"] == "unresolved"
    assert "no run state" in result["reason"]
    assert session.crm.patches == []


def test_an_event_with_no_token_at_all_falls_back_to_the_contact_id(store):
    session = session_with()
    result = events.handle_event(event_data("delivered", token=""),
                                 session=session, store=store)
    assert result["status"] == "recorded"
    assert result["object_id"] == "42"


def test_an_event_identifying_nothing_is_unresolved(store):
    result = events.handle_event(event_data("delivered", token="", object_id=""),
                                 session=session_with(), store=store)
    assert result["status"] == "unresolved"


def test_a_late_weaker_event_does_not_walk_the_status_back(sent):
    events.handle_event(event_data("clicked"), session=session_with(17), store=sent)
    session = session_with(27)
    result = events.handle_event(event_data("delivered"), session=session, store=sent)

    assert result["status"] == "superseded"
    assert result["current_status"] == "clicked"
    assert session.crm.patches == []            # nothing weaker is written


# ------------------------------------------------------------------ step 9
def test_campaign_complete_is_set_when_the_status_reaches_opened(sent):
    """Confirmed 2026-08-04: the campaign completes when lqabr_email_status
    is OPENED."""
    session = session_with(12)
    result = events.handle_event(event_data("opened"), session=session, store=sent)

    assert result["campaign_complete"] is True
    assert session.crm.patches[0][1]["lqabr_email_status"] == "OPENED"
    assert session.crm.patches[0][1]["email_campaign_complete"] is True


def test_a_click_also_completes_it_because_a_click_maps_to_opened(sent):
    """The confirmed HubSpot enumeration has no CLICKED value, so a click
    records as OPENED — one rule covers both."""
    session = session_with(17)
    result = events.handle_event(event_data("clicked"), session=session, store=sent)

    assert result["campaign_complete"] is True
    assert session.crm.patches[0][1]["lqabr_email_status"] == "OPENED"


def test_delivered_alone_does_not_complete_the_campaign(sent):
    """DELIVERED is not OPENED — the lead has not engaged yet."""
    session = session_with(10)
    result = events.handle_event(event_data("delivered"), session=session, store=sent)
    assert result["campaign_complete"] is False
    assert "email_campaign_complete" not in session.crm.patches[0][1]


def test_a_terminal_status_does_not_complete_the_campaign(sent):
    session = session_with(10)
    result = events.handle_event(event_data("failed", severity="permanent"),
                                 session=session, store=sent)
    assert result["campaign_complete"] is False
    assert "email_campaign_complete" not in session.crm.patches[0][1]


def test_the_handoff_is_the_column_alone_not_a_probability_threshold(sent):
    # probability 55 is well past the 30 text/voice threshold, but with no
    # click the campaign-complete column stays unset.
    session = session_with(55)
    result = events.handle_event(event_data("delivered"), session=session, store=sent)
    assert result["campaign_complete"] is False


def test_campaign_complete_is_written_once_not_on_every_later_event(sent):
    events.handle_event(event_data("opened"), session=session_with(10), store=sent)
    session = session_with(22)
    result = events.handle_event(event_data("clicked"), session=session, store=sent)
    assert result["campaign_complete"] is False    # already true, not rewritten
    assert "email_campaign_complete" not in session.crm.patches[0][1]


def test_a_permanent_failure_records_as_failed_and_ends_the_run(sent):
    session = session_with(12)
    result = events.handle_event(event_data("failed", severity="permanent"),
                                 session=session, store=sent)
    # Internally still classified as a permanent bounce (drives suppression
    # and precedence), but the HubSpot column records the single "not
    # workable" value FAILED rather than BOUNCED.
    assert result["event"] == "bounced"
    assert result["terminal"] is True
    assert session.crm.patches[0][1]["lqabr_email_status"] == "FAILED"
    assert "probability" not in session.crm.patches[0][1]   # terminals do not score


def test_a_transient_failure_records_as_failed(sent):
    session = session_with(12)
    result = events.handle_event(event_data("failed", severity="temporary"),
                                 session=session, store=sent)
    assert result["event"] == "failed" and result["terminal"] is True
    assert session.crm.patches[0][1]["lqabr_email_status"] == "FAILED"


def test_an_unsubscribe_is_terminal_and_recorded(sent):
    session = session_with(12)
    result = events.handle_event(event_data("unsubscribed"), session=session, store=sent)
    assert result["event"] == "unsubscribed" and result["terminal"] is True


def test_a_crm_failure_is_reported_so_mailgun_retries(sent):
    session = session_with(12)
    session.crm.raise_on_patch = CRMError("HubSpot 503")
    result = events.handle_event(event_data("delivered"), session=session, store=sent)
    assert result["status"] == "unresolved"
    assert result["reason"].startswith("crm-error")


def test_probability_is_read_back_from_hubspot_not_assumed(sent):
    """HubSpot is the system of record — on conflict CRM wins, so the
    increment is applied to whatever HubSpot currently holds."""
    session = session_with(probability=44)
    result = events.handle_event(event_data("delivered"), session=session, store=sent)
    assert result["probability"] == 46


# ------------------------------------------------ last_modified_email column
def test_last_modified_email_is_written_on_delivered(sent):
    """Every email event must stamp the 'Last Modified Email' datetime
    column so the portal reflects when activity actually landed."""
    session = session_with(probability=10)
    events.handle_event(event_data("delivered"), session=session, store=sent)
    patch_props = session.crm.patches[0][1]
    assert "last_modified_email" in patch_props
    ts = patch_props["last_modified_email"]
    assert isinstance(ts, int) and ts > 0


def test_last_modified_email_is_written_on_opened(sent):
    session = session_with(probability=10)
    events.handle_event(event_data("opened"), session=session, store=sent)
    assert "last_modified_email" in session.crm.patches[0][1]


def test_last_modified_email_is_written_on_terminal_status(sent):
    """Terminal events (BOUNCED, FAILED) also stamp the column — the send
    attempt itself is email activity worth recording."""
    session = session_with(probability=10)
    events.handle_event(event_data("failed", severity="permanent"),
                        session=session, store=sent)
    assert "last_modified_email" in session.crm.patches[0][1]


def test_last_modified_email_not_written_on_superseded_event(sent):
    """A weaker status that loses the resolution race does not write back
    at all — no patch means no timestamp column either."""
    events.handle_event(event_data("clicked"), session=session_with(17), store=sent)
    session = session_with(27)
    result = events.handle_event(event_data("delivered"), session=session, store=sent)
    # superseded → patch_object is never called for this second event
    assert result["status"] == "superseded"
    assert session.crm.patches == []
