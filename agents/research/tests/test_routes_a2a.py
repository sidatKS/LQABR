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


# --- the gateway mirrors every id at the top level, in both spellings -------
# agents/gateway/lib/soloai/protocols/a2a.py builds metadata AND, via its
# compat shim, object_id/objectId/summary_ref_id/summaryRefId at the top level.
# Metadata is authoritative; an id that resolves one way and not the other is
# a trap for the next caller.

def test_the_real_gateway_envelope_parses_both_ways():
    meta = {"trigger_id": "T-1", "object_id": "328843080440", "run_id": "gw-1",
            "route_id": "R-blog-summary", "source": "hubspot",
            "summary_ref_id": "328843080440"}
    env = A2AEnvelope(jsonrpc="2.0", id="x", method="message/send",
                      params={"metadata": meta},
                      object_id=meta["object_id"], objectId=meta["object_id"],
                      summary_ref_id=meta["summary_ref_id"],
                      summaryRefId=meta["summary_ref_id"])
    assert env.campaign_target().object_id == "328843080440"
    assert env.target().object_id == "328843080440"
    assert env.target().summary_ref_id == "328843080440"


def test_summary_ref_id_resolves_the_same_three_ways_as_object_id():
    assert A2AEnvelope(params={"metadata": {"summary_ref_id": "M"}}).target().summary_ref_id == "M"
    assert A2AEnvelope(summary_ref_id="S").target().summary_ref_id == "S"
    assert A2AEnvelope(summaryRefId="C").target().summary_ref_id == "C"


def test_metadata_wins_over_the_top_level_mirror():
    """The mirror is a compat shim; metadata is the contract."""
    env = A2AEnvelope(params={"metadata": {"object_id": "META",
                                           "summary_ref_id": "META-REF"}},
                      object_id="TOP", summaryRefId="TOP-REF")
    assert env.target().object_id == "META"
    assert env.target().summary_ref_id == "META-REF"


def test_a_stray_blog_published_at_is_ignored():
    """The gateway stopped sending it (dispatch.py, 2026-08-24) and the MCP
    stopped keying on it. An old caller sending it must not break the run."""
    env = A2AEnvelope(params={"metadata": {"object_id": "1",
                                           "summary_ref_id": "2",
                                           "blog_published_at": "2026-08-17T10:00:00Z"}})
    target = env.target()
    assert target.object_id == "1" and target.summary_ref_id == "2"
    assert not hasattr(target, "blog_published_at")
