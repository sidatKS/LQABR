"""The agent's own HTTP contract — what the gateway actually calls.

The point of this surface is that the gateway speaks ONE shape to every
stage agent. These tests pin that shape: the route names, the health
spellings, the id aliasing, and what each upstream failure looks like from
the outside.
"""

import pytest
from fastapi.testclient import TestClient

import outreach
import service_app
from lqabr_core.crm import CRMError
from mcp.hubspot.auth import TokenError
from runstate import RunStateError

CAMPAIGN_RESULT = {
    "object_id": "trg-1", "run_id": "run-9", "correlation_token": "trg-1:run-9",
    "lead_count": 2, "results": [{"status": "sent"}, {"status": "sent"}], "unresolved": [],
}


class Recorder:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else CAMPAIGN_RESULT
        self.error = error
        self.calls = []

    def __call__(self, object_id, **kwargs):
        self.calls.append({"object_id": object_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def campaign(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(outreach, "run_campaign", recorder)
    return recorder


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("LQABR_EMAIL_GATEWAY_TOKEN", raising=False)
    return TestClient(service_app.create_app(routes="all"))


# ------------------------------------------------------------------ liveness
def test_both_health_spellings_answer_identically(client):
    """The ADK runner answered /health; this agent's webhook answered
    /healthz. Serving both is the point — nothing downstream should have to
    know which spelling we picked."""
    health, healthz = client.get("/health"), client.get("/healthz")
    assert health.status_code == healthz.status_code == 200
    assert health.json() == healthz.json()
    assert health.json()["status"] == "ok"
    assert health.json()["service"] == "email_agent"


def test_root_describes_the_service_rather_than_404ing(client):
    """A smoke-test curl against / returned 404 on the ADK runner. That is
    what made the deployment look broken when it was merely undocumented."""
    body = client.get("/").json()
    assert body["service"] == "email_agent"
    assert "POST /hubspot/campaign" in body["routes"]
    assert "GET /health" in body["routes"] and "GET /healthz" in body["routes"]


# -------------------------------------------------------------------- step 2
def test_the_gateway_entry_point_runs_a_campaign(client, campaign):
    resp = client.post("/hubspot/campaign", json={"object_id": "trg-1"})
    assert resp.status_code == 200
    assert resp.json() == CAMPAIGN_RESULT
    assert campaign.calls[0]["object_id"] == "trg-1"


def test_trigger_id_is_accepted_as_an_alias(client, campaign):
    """The design doc, the gateway and the HubSpot property all say
    trigger_id; the agent says object_id. Failing an integration over a
    rename would be self-inflicted."""
    assert client.post("/hubspot/campaign", json={"trigger_id": "trg-2"}).status_code == 200
    assert campaign.calls[0]["object_id"] == "trg-2"


def test_object_id_wins_when_both_are_sent(client, campaign):
    client.post("/hubspot/campaign", json={"object_id": "a", "trigger_id": "b"})
    assert campaign.calls[0]["object_id"] == "a"


def test_limit_and_dry_run_are_passed_through(client, campaign):
    client.post("/hubspot/campaign", json={"object_id": "trg-1", "limit": 5, "dry_run": True})
    assert campaign.calls[0]["limit"] == 5
    assert campaign.calls[0]["dry_run"] is True


def test_a_request_with_no_id_is_a_400_not_a_run(client, campaign):
    assert client.post("/hubspot/campaign", json={}).status_code == 400
    assert client.post("/hubspot/campaign", json={"object_id": "   "}).status_code == 400
    assert campaign.calls == []


def test_blank_body_does_not_500(client, campaign):
    assert client.post("/hubspot/campaign", json={"limit": 3}).status_code == 400
    assert campaign.calls == []


# ---------------------------------------------------------- upstream failures
@pytest.mark.parametrize("error,expected", [
    (RunStateError("state dir not writable"), 503),
    (TokenError("secret manager unreachable"), 502),
    (CRMError("HubSpot 503"), 502),
])
def test_upstream_failures_map_to_distinguishable_statuses(monkeypatch, client, error, expected):
    """The gateway has to tell 'retry me' from 'my credentials are wrong'.
    A run-state failure is 503 because it is transient and the run never
    started; an auth or CRM failure is 502 because the fault is upstream."""
    monkeypatch.setattr(outreach, "run_campaign", Recorder(error=error))
    resp = client.post("/hubspot/campaign", json={"object_id": "trg-1"})
    assert resp.status_code == expected


def test_a_run_state_failure_never_reads_as_success(monkeypatch, client):
    """Sending mail whose engagement events could never be attributed is
    worse than not sending — this must not come back 200."""
    monkeypatch.setattr(outreach, "run_campaign", Recorder(error=RunStateError("read-only fs")))
    assert client.post("/hubspot/campaign", json={"object_id": "trg-1"}).status_code != 200


# ---------------------------------------------------------------- gateway auth
def test_no_token_configured_means_the_route_is_open_to_cloud_run_iam(client, campaign):
    assert client.post("/hubspot/campaign", json={"object_id": "trg-1"}).status_code == 200


def test_a_configured_token_is_required(monkeypatch, campaign):
    monkeypatch.setenv("LQABR_EMAIL_GATEWAY_TOKEN", "s3cret")
    guarded = TestClient(service_app.create_app(routes="all"))

    assert guarded.post("/hubspot/campaign", json={"object_id": "t"}).status_code == 401
    assert guarded.post("/hubspot/campaign", json={"object_id": "t"},
                        headers={"X-LQABR-Gateway-Token": "wrong"}).status_code == 401
    assert campaign.calls == []

    ok = guarded.post("/hubspot/campaign", json={"object_id": "t"},
                      headers={"X-LQABR-Gateway-Token": "s3cret"})
    assert ok.status_code == 200
    assert len(campaign.calls) == 1


def test_the_mailgun_route_is_not_behind_the_gateway_token(monkeypatch):
    """Mailgun cannot send our header. Its boundary is the HMAC, and adding
    a second one would just drop every real event."""
    from lqabr_core.secrets import get_secret
    get_secret.cache_clear()
    monkeypatch.setenv("LQABR_MAILGUN_WEBHOOK_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("LQABR_EMAIL_GATEWAY_TOKEN", "s3cret")
    guarded = TestClient(service_app.create_app(routes="all"))
    # No gateway token, bad signature -> 401 from the HMAC check, not the
    # header check; either way it is rejected, but by the right guard.
    resp = guarded.post("/mailgun/events",
                        json={"signature": {"timestamp": "1", "token": "t",
                                            "signature": "forged"},
                              "event-data": {"event": "delivered"}})
    assert resp.status_code == 401
    assert "Mailgun signature" in resp.json()["detail"]


# ------------------------------------------------------------- route selection
def test_a_campaign_only_deployment_hides_the_mailgun_entry():
    campaign_only = TestClient(service_app.create_app(routes="campaign"))
    assert campaign_only.get("/health").status_code == 200
    assert campaign_only.post("/mailgun/events", json={}).status_code == 404


def test_a_webhook_only_deployment_hides_the_campaign_entry():
    webhook_only = TestClient(service_app.create_app(routes="webhook"))
    assert webhook_only.get("/healthz").status_code == 200
    assert webhook_only.post("/hubspot/campaign", json={"object_id": "t"}).status_code == 404


def test_an_unknown_routes_mode_fails_loudly_at_startup(monkeypatch):
    monkeypatch.setenv("LQABR_EMAIL_ROUTES", "everything")
    with pytest.raises(RuntimeError):
        service_app.create_app()


def test_the_mode_is_reported_on_health():
    assert TestClient(service_app.create_app(routes="campaign")
                      ).get("/health").json()["routes"] == "campaign"


# -------------------------------------------------------------------- run state
def test_run_state_is_inspectable(client, store, monkeypatch):
    from runstate import LeadRunRecord
    monkeypatch.setattr(service_app, "RunStateStore", lambda *a, **k: store)
    store.record_send("trg-1", "run-9", LeadRunRecord(
        object_id="42", email="jane@acme.example", message_id="<m-1@mg>", skill="technology"))

    body = client.get("/runs/trg-1/run-9").json()
    assert "42" in body["leads"]
    assert body["messages"]["<m-1@mg>"] == "42"


# ------------------------------------------------------ the contract, in one test
def test_the_surface_matches_the_text_voice_shape():
    """Both stage agents must present the same kind of contract to the
    gateway: a domain POST, a health GET, no session handshake."""
    paths = {r.path for r in service_app.create_app(routes="all").routes}
    assert {"/hubspot/campaign", "/mailgun/events", "/engagement/sync",
            "/health", "/healthz", "/"} <= paths
    # and nothing ADK-shaped is required to reach it
    assert not any(p.startswith("/apps/") for p in paths)
    assert "/run" not in paths


def test_mailgun_and_the_gateway_share_one_service():
    """Swaroop, 15:08 — "your mail gun has to send back the event triggers
    back to the SAME agent". One app serves both entries; there is no second
    image and no /webhooks/* surface any more."""
    paths = {r.path for r in service_app.create_app(routes="all").routes}
    assert "/hubspot/campaign" in paths and "/mailgun/events" in paths
    assert not any(p.startswith("/webhooks") for p in paths)


def test_routes_default_to_one_service_serving_everything(monkeypatch):
    monkeypatch.delenv("LQABR_EMAIL_ROUTES", raising=False)
    assert service_app.routes_mode() == "all"


def test_there_is_no_separate_webhook_entry_point():
    """The second image is gone (Swaroop, 12:45 — "why do you guys create the
    webhook image?"). The entrypoint's `webhook` kind runs
    `uvicorn webhook_app:app`, so as long as this file does not exist an
    email webhook image cannot be resurrected by a build arg.

    Asserted on the file, not on `import webhook_app`: other agents still
    ship their own module of that name."""
    from pathlib import Path as _Path
    src = _Path(service_app.__file__).resolve().parent
    assert not (src / "webhook_app.py").exists()


# ------------------------------------------------------------ step 8, pushed
class Handler:
    """Substitutes events.handle_event so the push path is tested on its own
    two jobs: prove authenticity, then dispatch."""

    def __init__(self, result):
        self.result = result
        self.seen = []

    def __call__(self, event_data):
        self.seen.append(event_data)
        return self.result


SIGNING_KEY = "test-signing-key"


@pytest.fixture
def signed_client(monkeypatch):
    import hashlib
    import hmac as _hmac
    from lqabr_core.secrets import get_secret

    get_secret.cache_clear()
    monkeypatch.setenv("LQABR_MAILGUN_WEBHOOK_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.delenv("LQABR_EMAIL_GATEWAY_TOKEN", raising=False)

    def payload(event="opened", variables=None):
        timestamp, token = "100", "tok"
        signature = _hmac.new(SIGNING_KEY.encode(), f"{timestamp}{token}".encode(),
                              hashlib.sha256).hexdigest()
        if variables is None:
            variables = {"lqabr_correlation_token": "trg-1:run-1",
                         "lqabr_object_id": "42"}
        return {"signature": {"timestamp": timestamp, "token": token,
                              "signature": signature},
                "event-data": {"event": event, "timestamp": 1234.5,
                               "user-variables": variables}}

    yield TestClient(service_app.create_app(routes="all")), payload
    get_secret.cache_clear()


def test_a_verified_push_is_dispatched_to_step_8(signed_client, monkeypatch):
    client, payload = signed_client
    handler = Handler({"status": "recorded", "event": "opened", "object_id": "42"})
    monkeypatch.setattr(service_app, "_handle", handler)

    resp = client.post("/mailgun/events", json=payload("opened"))
    assert resp.status_code == 200 and resp.json()["status"] == "recorded"
    assert handler.seen[0]["event"] == "opened"


def test_a_forged_signature_is_rejected_before_anything_else(signed_client, monkeypatch):
    client, payload = signed_client
    handler = Handler({"status": "recorded"})
    monkeypatch.setattr(service_app, "_handle", handler)

    body = payload()
    body["signature"]["signature"] = "forged"
    assert client.post("/mailgun/events", json=body).status_code == 401
    assert handler.seen == []          # the handler never ran


def test_an_event_outside_the_vocabulary_is_acknowledged(signed_client, monkeypatch):
    client, payload = signed_client
    monkeypatch.setattr(service_app, "_handle",
                        Handler({"status": "ignored", "event": "accepted"}))
    resp = client.post("/mailgun/events", json=payload("accepted"))
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"


def test_an_unattributable_event_is_422_never_silently_dropped(signed_client, monkeypatch):
    client, payload = signed_client
    monkeypatch.setattr(service_app, "_handle",
                        Handler({"status": "unresolved",
                                 "reason": "no run state for this correlation token"}))
    assert client.post("/mailgun/events", json=payload("clicked")).status_code == 422


def test_a_crm_failure_is_500_so_mailgun_retries(signed_client, monkeypatch):
    client, payload = signed_client
    monkeypatch.setattr(service_app, "_handle",
                        Handler({"status": "unresolved", "reason": "crm-error: HubSpot 503"}))
    assert client.post("/mailgun/events", json=payload()).status_code == 500


# --------------------------------------------------------- step 8, tool call
def test_the_sync_route_makes_the_track_id_tool_call(client, monkeypatch):
    called = {}

    def fake_sync(object_id, run_id, **kwargs):
        called.update(object_id=object_id, run_id=run_id)
        return {"object_id": object_id, "run_id": run_id, "messages": 2,
                "events": 3, "recorded": 2, "results": []}

    monkeypatch.setattr(service_app.events_module, "sync_run_engagement", fake_sync)
    resp = client.post("/engagement/sync", json={"object_id": "trg-1", "run_id": "run-9"})

    assert resp.status_code == 200
    assert resp.json()["recorded"] == 2
    assert called == {"object_id": "trg-1", "run_id": "run-9"}


def test_the_sync_route_needs_a_run_id(client, monkeypatch):
    monkeypatch.setattr(service_app.events_module, "sync_run_engagement",
                        lambda *a, **k: {})
    assert client.post("/engagement/sync", json={"object_id": "trg-1"}).status_code == 400


def test_the_sync_route_is_behind_the_gateway_token(monkeypatch):
    monkeypatch.setenv("LQABR_EMAIL_GATEWAY_TOKEN", "s3cret")
    guarded = TestClient(service_app.create_app(routes="all"))
    resp = guarded.post("/engagement/sync", json={"object_id": "t", "run_id": "r"})
    assert resp.status_code == 401


# ------------------------------------------- credentials that cannot resolve
def test_an_unresolvable_credential_is_503_not_an_opaque_500(monkeypatch, client):
    """With LQABR_SECRETS_SOURCE=secret_manager the credential lookup happens
    at request time, so a missing role or absent ADC surfaces here. It must
    name the secret and be retryable — not read as a bug in the request."""
    from lqabr_core.secrets import SecretNotFoundError

    monkeypatch.setattr(outreach, "run_campaign",
                        Recorder(error=SecretNotFoundError(
                            "could not read lqabr-hubspot-access-token from Secret Manager")))
    resp = client.post("/hubspot/campaign", json={"object_id": "t"})
    assert resp.status_code == 503
    assert "secret-error" in resp.json()["detail"]
    assert "lqabr-hubspot-access-token" in resp.json()["detail"]


def test_a_mailgun_event_is_deferred_when_the_signing_key_cannot_be_resolved(monkeypatch):
    """Authenticity cannot be established, so the event must NOT be processed.
    5xx makes Mailgun retry, so it is deferred rather than lost."""
    from lqabr_core.secrets import SecretNotFoundError

    def unresolvable(*a, **k):
        raise SecretNotFoundError("lqabr-mailgun-webhook-signing-key unavailable")

    monkeypatch.setattr(service_app, "verify_webhook_signature", unresolvable)
    handler = Handler({"status": "recorded"})
    monkeypatch.setattr(service_app, "_handle", handler)

    app = TestClient(service_app.create_app(routes="all"))
    resp = app.post("/mailgun/events", json={"signature": {}, "event-data": {"event": "opened"}})

    assert resp.status_code == 503
    assert "secret-error" in resp.json()["detail"]
    assert handler.seen == []      # never processed without proving authenticity


def test_the_sync_route_reports_a_credential_fault_the_same_way(monkeypatch, client):
    from lqabr_core.secrets import SecretNotFoundError

    def boom(object_id, run_id, **kwargs):
        raise SecretNotFoundError("lqabr-mailgun-api-key unavailable")

    monkeypatch.setattr(service_app.events_module, "sync_run_engagement", boom)
    resp = client.post("/engagement/sync", json={"object_id": "t", "run_id": "r"})
    assert resp.status_code == 503 and "secret-error" in resp.json()["detail"]
