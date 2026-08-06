"""The ADK surface — that the tools wire to the right steps."""

import os

import pytest

import email_agent
import outreach
from email_fakes import FakeCRM, FakeMailgun, FakeSession
from lqabr_core.types import LeadProfile
from mcp.hubspot.schema import ValidatedProfile


def profile(object_id="42"):
    return ValidatedProfile(object_id=object_id, email_id="jane@acme.example",
                            first_name="Jane", last_name="Smith", employee_id="E00002",
                            job_title="VP Engineering", company_id="C-1",
                            industry="Software", probability=10)


def lead(object_id="42"):
    return LeadProfile(external_employee_id="E00002", email="jane@acme.example",
                       external_company_id="C-1",
                       job_title="VP Engineering", probability=10,
                       hubspot_contact_id=object_id, extra={"email_status": "PENDING"})


@pytest.fixture
def wired(monkeypatch, store, fake_model_fn):
    """Point every tool at a fake MCP session, Mailgun and model.

    `build_model_fn` is stubbed to the fake rather than to None: construction
    is instruction-based, so None now raises instead of falling back to a
    template."""
    crm = FakeCRM(profiles={"42": profile()}, leads=[lead()])
    session = FakeSession(crm)
    monkeypatch.setattr(email_agent, "build_session", lambda **kwargs: session)
    monkeypatch.setattr(outreach, "build_session", lambda **kwargs: session)
    mailgun = FakeMailgun()
    monkeypatch.setattr(outreach, "MailgunClient", lambda *a, **k: mailgun)
    monkeypatch.setattr(outreach, "RunStateStore", lambda *a, **k: store)
    monkeypatch.setattr(outreach, "build_model_fn", lambda ctx, *a: fake_model_fn)
    return session, mailgun


def test_root_agent_exposes_the_campaign_entry_point():
    names = {tool.__name__ for tool in email_agent.root_agent.tools}
    assert "run_email_campaign" in names
    assert {"preview_email", "send_outreach_email", "get_lead_status",
            "get_lead_profile", "refresh_lead_status", "list_email_queue",
            "get_run_state"} <= names


def test_run_email_campaign_sends_one_email_per_lead(wired):
    session, mailgun = wired
    result = email_agent.run_email_campaign("trg-100")
    assert result["object_id"] == "trg-100"
    assert result["lead_count"] == 1
    assert len(mailgun.sends) == 1
    assert result["results"][0]["status"] == "sent"


def test_dry_run_reports_without_sending(wired):
    _, mailgun = wired
    result = email_agent.run_email_campaign("trg-101", dry_run=True)
    assert result["results"][0]["status"] == "dry-run"
    assert mailgun.sends == []


def test_preview_shows_the_skill_and_copy_without_sending(wired):
    _, mailgun = wired
    result = email_agent.preview_email("42")
    assert result["skill"] == "technology"
    assert "C-1" in result["subject"]
    assert mailgun.sends == []


def test_send_to_a_named_lead_uses_the_full_step_4_to_7_path(wired):
    session, mailgun = wired
    result = email_agent.send_outreach_email("42")
    assert result["status"] == "sent"
    assert result["run_id"]
    assert session.bearer_calls == 1
    assert mailgun.sends[0]["to"] == "jane@acme.example"


def test_get_lead_profile_surfaces_only_the_named_fields(wired):
    _, mailgun = wired
    result = email_agent.get_lead_profile("42")
    # Exactly these keys, nothing else.
    assert set(result) == {"object_id", "email_id", "first_name", "last_name",
                           "employee_id", "job_title", "industry", "company_id"}
    assert result["object_id"] == "42"
    assert result["email_id"] == "jane@acme.example"
    assert result["first_name"] == "Jane" and result["last_name"] == "Smith"
    # Excluded fields must never leak — especially missing_pointers.
    for absent in ("probability", "email_status", "missing_pointers", "phone",
                   "location", "linkedin_url", "company_size_revenue"):
        assert absent not in result
    assert mailgun.sends == []


def test_an_unknown_object_id_is_an_error_not_a_send(wired, monkeypatch):
    from lqabr_core.crm import CRMError

    session, mailgun = wired

    def missing(object_id):
        raise CRMError("HubSpot GET /crm/v3/objects/contacts/999 failed: HTTP 404")

    monkeypatch.setattr(session.crm, "get_lead_profile", missing)
    result = email_agent.send_outreach_email("999")
    assert "crm-error" in result["error"]
    assert mailgun.sends == []


def test_get_lead_status_is_read_only(wired):
    _, mailgun = wired
    status = email_agent.get_lead_status("42")
    assert status["object_id"] == "42"
    assert status["email_status"] == "PENDING"
    assert status["probability"] == 10
    assert mailgun.sends == []


def test_refresh_lead_status_pulls_live_then_reads(wired, monkeypatch):
    session, mailgun = wired
    # The lead was sent in exactly one run; refresh must pull that run.
    monkeypatch.setattr(
        email_agent.RunStateStore, "runs_for_object",
        lambda self, object_id: [("trg-9", "run-9")] if str(object_id) == "42" else [])

    calls = []

    def fake_sync(object_id, run_id):
        calls.append((object_id, run_id))
        # Simulate the write-back landing OPENED on the HubSpot record that
        # refresh_lead_status re-reads (by object id) after the pull.
        session.crm.profiles["42"].email_status = "OPENED"
        return {"events": 1, "recorded": 1}

    monkeypatch.setattr(email_agent.events_module, "sync_run_engagement", fake_sync)

    result = email_agent.refresh_lead_status("42")
    assert calls == [("trg-9", "run-9")]
    assert result["email_status"] == "OPENED"
    assert result["runs_pulled"][0]["recorded"] == 1
    assert mailgun.sends == []


def test_refresh_lead_status_without_run_state_is_a_plain_read(wired, monkeypatch):
    session, mailgun = wired
    monkeypatch.setattr(email_agent.RunStateStore, "runs_for_object",
                        lambda self, object_id: [])
    result = email_agent.refresh_lead_status("42")
    assert result["email_status"] == "PENDING"
    assert result["runs_pulled"] == []
    assert mailgun.sends == []


def test_list_email_queue_reports_the_trigger_batch(wired):
    queue = email_agent.list_email_queue(object_id="trg-100")
    assert queue["count"] == 1 and queue["object_id"] == "trg-100"


def test_adk_can_load_the_agent_as_a_package():
    """`adk web agents/email/src` imports this as a PACKAGE (`src.agent`),
    which puts agents/email/ on sys.path but NOT agents/email/src/.

    Every module in src/ imports its siblings flat, so without the path
    insert in agent.py this fails with `No module named 'observability'`.
    No other test catches it: conftest adds src/ to sys.path, so the whole
    suite runs under the one path layout that already works.

    Run in a subprocess so the import happens with a clean sys.path.
    """
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    agent_root = _Path(__file__).resolve().parents[1]        # agents/email
    repo_root = agent_root.parents[1]

    script = (
        "import sys, types\n"
        "m = types.ModuleType('google.adk.agents')\n"
        "m.Agent = type('Agent', (), {'__init__': lambda s, **k: None})\n"
        "sys.modules.setdefault('google', types.ModuleType('google'))\n"
        "sys.modules['google.adk'] = types.ModuleType('google.adk')\n"
        "sys.modules['google.adk.agents'] = m\n"
        "import importlib\n"
        "mod = importlib.import_module('src.agent')\n"
        "assert mod.root_agent is not None\n"
        "print('OK')\n"
    )
    proc = subprocess.run([_sys.executable, "-c", script], cwd=str(agent_root),
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(repo_root)})
    assert "OK" in proc.stdout, (
        f"ADK-style package import failed:\n{proc.stderr[-1500:]}")
