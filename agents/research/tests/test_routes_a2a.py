"""The gateway's two doors: one carries a CONTACT id, one carries a POST id.

Sending a post to the contact route is the failure this route split exists to
prevent, so the tests pin which handler each envelope reaches.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from schema import A2AEnvelope


@pytest.fixture
def client(monkeypatch):
    """The real app. The MCP is not reachable here and does not need to be —
    both routes answer before any CRM work starts."""
    monkeypatch.setenv("LQABR_RESEARCH_MCP_STARTUP_CHECK", "off")
    import service_app
    with TestClient(service_app.app) as running:
        yield running


def _envelope(**metadata):
    return A2AEnvelope(jsonrpc="2.0", id="req-1", method="message/send",
                       params={"metadata": metadata})


def test_the_post_id_becomes_the_campaign_target():
    """On the blog-summary route the gateway's objectId IS the post."""
    target = _envelope(objectId="330008697562").campaign_target()
    assert target.objectId == "330008697562"
    assert target.limit == 100
    assert not hasattr(target, "industry")   # it comes off the post


def test_the_gateway_can_override_the_limit():
    target = _envelope(objectId="330008697562", limit=5).campaign_target()
    assert target.limit == 5


def test_an_industry_in_metadata_is_ignored():
    """The gateway cannot send one — `industry` is not in its
    ALLOWED_METADATA_KEYS and an unlisted key makes the dispatch raise — and
    the industry belongs to the post. An envelope carrying one is ignored."""
    target = _envelope(objectId="330008697562",
                       industry="FINANCIAL_SERVICES").campaign_target()
    assert not hasattr(target, "industry")


def test_a_top_level_object_id_is_read_too():
    """The gateway mirrors the id outside metadata for older agents."""
    envelope = A2AEnvelope(jsonrpc="2.0", objectId="330008697562")
    assert envelope.campaign_target().objectId == "330008697562"
    assert envelope.target().objectId == "330008697562"


def test_an_empty_envelope_yields_no_target_rather_than_a_wrong_one():
    """No id must reject, never default to researching some other record."""
    assert _envelope().campaign_target().objectId == ""
    assert _envelope().target().objectId == ""


def test_the_two_readings_share_the_id_but_not_the_meaning():
    """Same wire field, different record type — that is the whole point."""
    envelope = _envelope(objectId="330008697562", summary_objectId="329605630651")
    assert envelope.campaign_target().objectId == "330008697562"   # the POST
    assert envelope.target().objectId == "330008697562"            # a CONTACT
    assert envelope.target().summary_objectId == "329605630651"


def test_the_campaign_route_is_the_only_hand_off():
    """The gateway's agents_registry.yaml has exactly one research route —
    R-blog-summary, ticket.propertyChange on blog_summary. Nothing dispatches
    a single contact here, so no route exists for one."""
    from research_core.settings import get_settings
    import service_app
    settings = get_settings(refresh=True)
    assert settings.route_campaign_a2a == "/research/campaign/a2a"
    assert not hasattr(settings, "route_a2a")
    paths = {getattr(r, "path", "") for r in service_app.app.routes}
    assert "/research/a2a" not in paths
    assert settings.route_campaign_a2a in paths


# --- the gateway mirrors every id at the top level, in both spellings -------
# agents/gateway/lib/soloai/protocols/a2a.py builds metadata AND, via its
# compat shim, object_id/objectId/summary_objectId/summaryRefId at the top level.
# Metadata is authoritative; an id that resolves one way and not the other is
# a trap for the next caller.

def test_the_real_gateway_envelope_parses_both_ways():
    meta = {"trigger_id": "T-1", "object_id": "328843080440", "run_id": "gw-1",
            "route_id": "R-blog-summary", "source": "hubspot",
            "summary_objectId": "328843080440"}
    env = A2AEnvelope(jsonrpc="2.0", id="x", method="message/send",
                      params={"metadata": meta},
                      objectId=meta["object_id"],
                      summary_objectId=meta["summary_objectId"],
                      summaryRefId=meta["summary_objectId"])
    assert env.campaign_target().objectId == "328843080440"
    assert env.target().objectId == "328843080440"
    assert env.target().summary_objectId == "328843080440"


def test_summary_ref_id_resolves_the_same_three_ways_as_object_id():
    assert A2AEnvelope(params={"metadata": {"summary_objectId": "M"}}).target().summary_objectId == "M"
    assert A2AEnvelope(summary_objectId="S").target().summary_objectId == "S"
    assert A2AEnvelope(summaryRefId="C").target().summary_objectId == "C"


def test_metadata_wins_over_the_top_level_mirror():
    """The mirror is a compat shim; metadata is the contract."""
    env = A2AEnvelope(params={"metadata": {"objectId": "META",
                                           "summary_objectId": "META-REF"}},
                      objectId="TOP", summaryRefId="TOP-REF")
    assert env.target().objectId == "META"
    assert env.target().summary_objectId == "META-REF"


def test_a_stray_blog_published_at_is_ignored():
    """The gateway stopped sending it (dispatch.py, 2026-08-24) and the MCP
    stopped keying on it. An old caller sending it must not break the run."""
    env = A2AEnvelope(params={"metadata": {"objectId": "1",
                                           "summary_objectId": "2",
                                           "blog_published_at": "2026-08-17T10:00:00Z"}})
    target = env.target()
    assert target.objectId == "1" and target.summary_objectId == "2"
    assert not hasattr(target, "blog_published_at")


# --- the gateway forwards HubSpot's own webhook shape ----------------------
# Seen live 2026-08-25: `objectId`, `subscriptionType`, `attemptNumber` — the
# raw event, not the A2A envelope. The id already resolved; what did not was
# telling a Ticket from a Contact, which is the one mix-up this agent cannot
# recover from (a post sent to the contact route dies at read_lead with a CRM
# error that reads like a record went missing).

WEBHOOK = {
    "objectId": "329213149924",
    "propertyName": "blog_summary",
    "propertyValue": "hi this is srinivas , how are you doing , ",
    "subscriptionType": "ticket.propertyChange",
    "portalId": 246777241,
    "eventId": "3425256010",
    "occurredAt": 1787652854704,
    "attemptNumber": 0,
    "changeSource": "CRM_UI",
    "triggerId": "trg-f524fec9cdd65a4395f3ec5b",
}


def test_a_raw_hubspot_webhook_resolves_its_id():
    from schema import A2AEnvelope
    assert A2AEnvelope(**WEBHOOK).campaign_target().objectId == "329213149924"


def test_a_ticket_event_names_itself_a_post():
    from schema import A2AEnvelope
    assert A2AEnvelope(**WEBHOOK).record_kind() == "post"
    assert A2AEnvelope(**{**WEBHOOK,
                          "subscriptionType": "contact.propertyChange"}
                       ).record_kind() == "contact"
    assert A2AEnvelope(objectId="1").record_kind() == "", "silence is not a guess"


def test_only_a_redelivery_is_reported():
    """attemptNumber=0 is every first delivery; printing it teaches nothing."""
    from schema import A2AEnvelope
    assert "attempt" not in A2AEnvelope(**WEBHOOK).source()
    assert A2AEnvelope(**{**WEBHOOK, "attemptNumber": 2}).source()["attempt"] == 2
    assert A2AEnvelope(**WEBHOOK).source()["subscription_type"] == "ticket.propertyChange"
    assert A2AEnvelope(**WEBHOOK).source()["event_id"] == "3425256010"


def test_a_contact_event_is_refused_at_the_door(client):
    """The inverse mix-up: a contact hand-off reaching the campaign route.
    Refused here, not three steps later as `crm-error: no blog summary`."""
    contact = {**WEBHOOK, "subscriptionType": "contact.propertyChange",
               "propertyName": "lead_context"}
    envelope = client.post("/research/campaign/a2a", json=contact).json()
    # A refusal is a TOP-LEVEL JSON-RPC error. It used to sit under `result`,
    # and the gateway scores any 2xx without a top-level `error` as a
    # successful dispatch — so a refused hand-off was recorded as delivered.
    # Acceptances still carry `result`; only refusals moved.
    assert "result" not in envelope
    body = envelope["error"]["data"]
    assert body["status"] == "rejected"
    assert "takes a post" in body["reason"] and "is a contact" in body["reason"]
    assert "agent.py" in body["reason"], "say what to use for one lead"


def test_the_single_contact_route_no_longer_exists(client):
    assert client.post("/research/a2a", json=WEBHOOK).status_code == 404


def test_the_same_event_is_accepted_on_the_campaign_route(client):
    body = client.post("/research/campaign/a2a", json=WEBHOOK).json()["result"]
    assert body["status"] == "accepted"
    assert body["objectId"] == "329213149924" and body["mode"] == "campaign"


# --- the gateway's real envelope: JSON-RPC outside, HubSpot's event inside --
# Seen live 2026-08-25. The metadata is HubSpot's own, so it is camelCase.
# Reading only `objectId` there rejected a hand-off that had a perfectly good
# id: "payload carries no objectId".

GATEWAY_ENVELOPE = {
    "jsonrpc": "2.0",
    "id": "2b2a2857-191d-4d0e-9f4e-468b0428ba5d",
    "method": "message/send",
    "params": {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "trg-837dd204bd6a5811b410b20d"}],
            "messageId": "4f7a27d1-96c6-4d16-8cc4-d751ce2ec54c",
        },
        "metadata": {
            "objectId": "329213149924",
            "propertyName": "blog_summary",
            "propertyValue": "hi this is srinivas , how are you doing , ",
            "subscriptionType": "ticket.propertyChange",
            "portalId": 246777241,
            "eventId": "1107748449",
            "occurredAt": 1787651383486,
            "attemptNumber": 0,
            "changeSource": "CRM_UI",
            "triggerId": "trg-837dd204bd6a5811b410b20d",
        },
    },
}


def test_the_gateways_camelcase_metadata_resolves_its_id():
    envelope = A2AEnvelope(**GATEWAY_ENVELOPE)
    assert envelope.campaign_target().objectId == "329213149924"
    assert envelope.record_kind() == "post"
    assert envelope.source()["event_id"] == "1107748449"


def test_snake_case_metadata_still_resolves():
    """The older spelling must keep working — this is a widening, not a swap."""
    assert _envelope(objectId="330008697562", limit=5).campaign_target() \
        .objectId == "330008697562"
    assert _envelope(objectId="1", summary_objectId="2").target().summary_objectId == "2"
    assert _envelope(run_id="res-abc").run_id() == "res-abc"


def test_camelcase_overrides_are_read_too():
    envelope = A2AEnvelope(params={"metadata": {
        "objectId": "1", "summaryRefId": "2", "runId": "res-xyz", "limit": 5}})
    assert envelope.target().summary_objectId == "2"
    assert envelope.run_id() == "res-xyz"
    assert envelope.campaign_target().limit == 5


def test_the_real_envelope_is_accepted_by_the_campaign_route(client):
    body = client.post("/research/campaign/a2a", json=GATEWAY_ENVELOPE).json()
    assert body["id"] == GATEWAY_ENVELOPE["id"], "the JSON-RPC id must come back"
    assert body["result"]["status"] == "accepted"
    assert body["result"]["objectId"] == "329213149924"
