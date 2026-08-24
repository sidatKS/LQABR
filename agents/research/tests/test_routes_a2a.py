"""The gateway's two doors: one carries a CONTACT id, one carries a POST id.

Sending a post to the contact route is the failure this route split exists to
prevent, so the tests pin which handler each envelope reaches.
"""

from __future__ import annotations

from schema import A2AEnvelope


def _envelope(**metadata):
    return A2AEnvelope(jsonrpc="2.0", id="req-1", method="message/send",
                       params={"metadata": metadata})


def test_the_post_id_becomes_the_campaign_target():
    """On the blog-summary route the gateway's object_id IS the post."""
    target = _envelope(object_id="330008697562").campaign_target()
    assert target.object_id == "330008697562"
    assert target.industry == ""          # comes off the post, not the envelope
    assert target.limit == 100


def test_the_gateway_can_override_industry_and_limit():
    target = _envelope(object_id="330008697562",
                       industry="FINANCIAL_SERVICES", limit=5).campaign_target()
    assert target.industry == "FINANCIAL_SERVICES"
    assert target.limit == 5


def test_a_top_level_object_id_is_read_too():
    """The gateway mirrors the id outside metadata for older agents."""
    envelope = A2AEnvelope(jsonrpc="2.0", object_id="330008697562")
    assert envelope.campaign_target().object_id == "330008697562"
    assert envelope.target().object_id == "330008697562"


def test_an_empty_envelope_yields_no_target_rather_than_a_wrong_one():
    """No id must reject, never default to researching some other record."""
    assert _envelope().campaign_target().object_id == ""
    assert _envelope().target().object_id == ""


def test_the_two_readings_share_the_id_but_not_the_meaning():
    """Same wire field, different record type — that is the whole point."""
    envelope = _envelope(object_id="330008697562", summary_ref_id="329605630651")
    assert envelope.campaign_target().object_id == "330008697562"   # the POST
    assert envelope.target().object_id == "330008697562"            # a CONTACT
    assert envelope.target().summary_ref_id == "329605630651"


def test_the_campaign_route_has_its_own_configurable_path():
    from research_core.settings import get_settings
    settings = get_settings(refresh=True)
    assert settings.route_campaign_a2a == "/research/campaign/a2a"
    assert settings.route_campaign_a2a != settings.route_a2a
