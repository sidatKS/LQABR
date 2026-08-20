"""Step 4 (Rev 5) — audience resolution.

One blog-summary Ticket -> every lead in that industry. These tests use an
in-memory HubSpot reader (FakeHubSpotReader), so they exercise the real
resolver and the real grouped hand-off with no network.
"""

from __future__ import annotations

import json

from conftest import gw_router, gw_audience, gw_dispatch, make_event, FakeHubSpotReader


def _blog_event(text="A healthcare post", ticket="328791966455", eid="e1"):
    return make_event("blog_summary", text, object_id=ticket,
                      subscription_type="ticket.propertyChange", event_id=eid)


def _a2a_client(session):
    from soloai.protocols.a2a import A2AClient
    return A2AClient(session=session, backoff_seconds=0, sleep=lambda _s: None)


class TestResolveOne:
    def test_five_leads_for_healthcare(self, router, audience):
        res = router.route_batch([_blog_event()])
        assert res.decisions[0].route_id == "R-blog-summary"
        leads, outcome = audience.resolve_one(res.decisions[0])
        assert [l.object_id for l in leads] == ["701", "702", "703", "704", "705"]
        assert all(l.summary_ref_id == "328791966455" for l in leads)
        assert all(l.agent == "research" for l in leads)
        assert (outcome.industry, outcome.company_count, outcome.lead_count) == \
            ("HEALTHCARE", 5, 5)

    def test_lead_trigger_ids_deterministic_and_unique(self, router, audience):
        res = router.route_batch([_blog_event()])
        a, _ = audience.resolve_one(res.decisions[0])
        b, _ = audience.resolve_one(res.decisions[0])
        assert [l.trigger_id for l in a] == [l.trigger_id for l in b]  # redelivery-stable
        assert len({l.trigger_id for l in a}) == 5                      # one per lead
        assert all(l.trigger_id.startswith("trg-") for l in a)


class TestExpand:
    def test_expand_replaces_blog_with_per_lead(self, router, audience, audit):
        out = audience.expand(router.route_batch([_blog_event()]), "run-1", audit)
        assert len(out.decisions) == 5
        assert {d.object_id for d in out.decisions} == {"701", "702", "703", "704", "705"}
        assert not out.errors

    def test_non_blog_decisions_pass_through_untouched(self, router, audience, audit):
        res = router.route_batch([make_event("lead_context", "ctx",
                                              object_id="900", event_id="x1")])
        out = audience.expand(res, "run-1", audit)
        assert len(out.decisions) == 1
        assert out.decisions[0].object_id == "900"
        assert out.decisions[0].summary_ref_id is None

    def test_unknown_industry_is_zero_audience_not_error(self, router, audit):
        resolver = gw_audience.AudienceResolver(FakeHubSpotReader())
        out = resolver.expand(router.route_batch([_blog_event()]), "run-1", audit)
        assert out.decisions == [] and out.errors == []

    def test_industry_with_no_companies_is_zero_audience(self, router, audit):
        resolver = gw_audience.AudienceResolver(FakeHubSpotReader(
            industry_by_ticket={"328791966455": "HEALTHCARE"},
            companies_by_industry={"HEALTHCARE": []}))
        out = resolver.expand(router.route_batch([_blog_event()]), "run-1", audit)
        assert out.decisions == [] and out.errors == []

    def test_hubspot_failure_becomes_a_routing_error(self, router, audit):
        resolver = gw_audience.AudienceResolver(FakeHubSpotReader(fail=True))
        out = resolver.expand(router.route_batch([_blog_event()]), "run-1", audit)
        assert out.decisions == []
        assert len(out.errors) == 1
        assert "audience resolution failed" in str(out.errors[0])


class TestGroupedHandoff:
    """The whole point: N same-industry leads travel in ONE call."""

    def test_five_leads_one_grouped_call_with_summary_ref(
            self, router, audience, audit, fake_session_factory):
        expanded = audience.expand(router.route_batch([_blog_event()]), "run-1", audit)
        session = fake_session_factory()
        d = gw_dispatch.Dispatcher(_a2a_client(session), audit, mode="grouped")
        outcomes = d.dispatch_grouped(expanded.decisions, "run-1")

        assert len(session.calls) == 1, "5 healthcare leads -> ONE call"
        md = session.calls[0]["json"]["params"]["metadata"]
        assert md["object_ids"] == ["701", "702", "703", "704", "705"]
        assert md["summary_ref_id"] == "328791966455"
        assert md["batch_size"] == 5
        assert outcomes[0].batch_size == 5 and outcomes[0].ok

    def test_two_tickets_are_not_merged(self, router, audit, fake_session_factory):
        resolver = gw_audience.AudienceResolver(FakeHubSpotReader(
            industry_by_ticket={"111": "HEALTHCARE", "222": "LEGAL_SERVICES"},
            companies_by_industry={"HEALTHCARE": ["c1"], "LEGAL_SERVICES": ["d1"]},
            contacts_by_company={"c1": ["701", "702"], "d1": ["801"]}))
        events = [_blog_event(ticket="111", eid="e1"), _blog_event(ticket="222", eid="e2")]
        expanded = resolver.expand(router.route_batch(events), "run-1", audit)
        session = fake_session_factory()
        d = gw_dispatch.Dispatcher(_a2a_client(session), audit, mode="grouped")
        d.dispatch_grouped(expanded.decisions, "run-1")
        assert len(session.calls) == 2, "two tickets -> two calls, never merged"

    def test_no_profile_field_reaches_the_wire(
            self, router, audience, audit, fake_session_factory):
        expanded = audience.expand(router.route_batch([_blog_event()]), "run-1", audit)
        session = fake_session_factory()
        d = gw_dispatch.Dispatcher(_a2a_client(session), audit, mode="grouped")
        d.dispatch_grouped(expanded.decisions, "run-1")
        blob = json.dumps(session.calls[0]["json"]).lower()
        for field in ("email", "phone", "full_name", "first_name", "company",
                      "annual_revenue", "job_title"):
            assert field not in blob
