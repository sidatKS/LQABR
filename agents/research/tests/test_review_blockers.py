"""Regressions for the six blockers found in the 2026-08-25 pre-merge review.

Each test names the defect it pins. They are grouped here rather than scattered
so the next reviewer can see at a glance which findings are closed.
"""

from __future__ import annotations

import inspect
import time

import pytest
from fastapi.testclient import TestClient

from composer import Composer
from conftest import FakeMCPClient, FakeSearch
from pipeline import run_campaign, run_research
from research_core.mcp.hubspot import HubSpotMCP
from research_core.search.anthropic_search import AnthropicWebSearch
from research_core.settings import get_settings
from schema import A2AEnvelope, CampaignTarget, ResearchTarget

BLOG = {"found": True, "ticket_hs_id": "T1", "summary": {
    "blog_summary": "Governed AI needs citations and sign-off.",
    "blog_industry": "HEALTHCARE", "blog_published_at": "2026-08-27T09:30:00Z"}}
LEAD = {"employee_id": "E1", "company_id": "C1", "decision_maker_flag": "Yes",
        "industry": "HEALTHCARE", "company": "Axiom Law", "first_name": "Mahesh"}
#: The MCP returns a lead the write tool cannot accept — no company_id.
UNWRITABLE = {**LEAD, "company_id": ""}


# --- BL-1 · blocking work must not run on the event loop -------------------

def test_the_blocking_route_is_not_async():
    """An `async def` handler runs ON the event loop, so blocking I/O in one
    freezes the process — /health included, and the a2a ack the gateway is
    waiting on inside ~5s. `mcp_tools` dials the MCP; it must stay a plain
    `def` so Starlette offloads it to a threadpool."""
    import service_app
    assert not inspect.iscoroutinefunction(service_app.mcp_tools)


def test_health_answers_while_a_blocking_handler_is_in_flight(monkeypatch):
    """The whole point of BL-1: a slow handler must not take the process."""
    monkeypatch.setenv("LQABR_RESEARCH_MCP_STARTUP_CHECK", "off")
    import service_app

    class _SlowMCP:
        def __init__(self, *a, **kw):
            pass

        def ensure_ready(self, *a, **kw):
            # SETTINGS is captured at import, so the startup check may still be
            # "warn" whatever this test sets in the environment. Answer it.
            return []

        @property
        def client(self):
            class _C:
                def list_tools(self):
                    time.sleep(1.0)      # stand-in for a sleepy container
                    return []
            return _C()

    monkeypatch.setattr(service_app, "HubSpotMCP", _SlowMCP)
    with TestClient(service_app.app) as client:
        import threading
        started = threading.Event()

        def _fire():
            started.set()
            client.get("/mcp/tools")

        worker = threading.Thread(target=_fire, daemon=True)
        worker.start()
        started.wait()
        time.sleep(0.15)
        began = time.monotonic()
        assert client.get("/health").status_code == 200
        waited = time.monotonic() - began
        worker.join(timeout=5)
    assert waited < 0.6, f"/health queued behind the blocking handler ({waited:.2f}s)"


# --- BL-3 · a write that never happened must not report success ------------

def _campaign(results, leads):
    settings = get_settings(refresh=True)
    hubspot = HubSpotMCP(client=FakeMCPClient(results), settings=settings)
    hubspot.list_leads_by_industry = lambda industry, limit=100: list(leads)
    return run_campaign(CampaignTarget(objectId="T1"), settings=settings,
                        hubspot=hubspot,
                        composer=Composer(provider=FakeSearch(), settings=settings))


def test_a_lead_the_write_tool_cannot_accept_is_a_failure():
    settings = get_settings(refresh=True)
    hubspot = HubSpotMCP(client=FakeMCPClient(
        {"get_lead_profile": UNWRITABLE, "get_blog_summary": BLOG}), settings=settings)
    response = run_research(
        ResearchTarget(objectId="1", summary_objectId="T1"), settings=settings,
        hubspot=hubspot,
        composer=Composer(provider=FakeSearch(), settings=settings))

    assert response.status == "failed", "a note that was never landed is not completed"
    assert "bad-data" in response.error, "the reason must be at the TOP level"
    assert response.hubspot.status == "not_writable"
    assert response.note, "the work still comes back so it is not lost"


def test_a_campaign_that_wrote_nothing_does_not_report_completed():
    """`written=0 skipped=2 failed=0 status=completed error=''` was the bug."""
    result = _campaign({"get_blog_summary": BLOG, "get_lead_profile": UNWRITABLE},
                       ["1", "2"])
    assert result.written == 0
    assert result.failed == 2 and result.skipped == 0
    assert result.status == "failed" and result.error


def test_context_already_present_is_still_a_success(monkeypatch):
    """The other half of the distinction: nothing NEEDED doing."""
    monkeypatch.setenv("LQABR_RESEARCH_SKIP_IF_CONTEXT_PRESENT", "1")
    settings = get_settings(refresh=True)
    hubspot = HubSpotMCP(client=FakeMCPClient(
        {"get_lead_profile": {**LEAD, "lead_context": "already written"},
         "get_blog_summary": BLOG}), settings=settings)
    response = run_research(
        ResearchTarget(objectId="1", summary_objectId="T1"), settings=settings,
        hubspot=hubspot,
        composer=Composer(provider=FakeSearch(), settings=settings))
    assert response.status == "completed" and response.hubspot.status == "skipped"


# --- BL-4 · gateway metadata is whatever HubSpot sent ----------------------

@pytest.mark.parametrize("limit,expected", [
    ("all", 100), (None, 100), ("", 100), ("0", 1), ("-5", 1),
    ("5", 5), (5, 5), ("99999", 1000),
])
def test_a_junk_limit_is_a_default_not_a_500(limit, expected):
    meta = {"objectId": "1"}
    if limit is not None:
        meta["limit"] = limit
    assert A2AEnvelope(params={"metadata": meta}).campaign_target().limit == expected


def test_a_junk_attempt_number_is_not_a_500():
    env = A2AEnvelope(params={"metadata": {"objectId": "1", "attemptNumber": "first"}})
    assert "attempt" not in env.source()


# --- BL-5 · a background run that raises must not vanish -------------------

def test_a_crashing_background_run_is_flagged_with_its_reason(monkeypatch, caplog):
    """The gateway already holds `accepted`; without this the run vanishes."""
    monkeypatch.setenv("LQABR_RESEARCH_MCP_STARTUP_CHECK", "off")
    import service_app

    def _boom(target, *, run_id=""):
        raise ValueError("LQABR_RESEARCH_MAX_TOKENS must be an integer")

    monkeypatch.setattr(service_app, "run_campaign", _boom)
    with caplog.at_level("INFO", logger="lqabr.research"):
        with TestClient(service_app.app) as client:
            with pytest.raises(ValueError):
                client.post("/research/campaign/a2a",
                            json={"objectId": "329473274558"})

    crashed = [r for r in caplog.records if '"event": "run_crashed"' in r.message]
    assert crashed, "a background run that raised left no record at all"
    assert "MAX_TOKENS must be an integer" in crashed[-1].message
    assert "329473274558" in crashed[-1].message, "say WHICH run died"


# --- BL-6 · a switch that does nothing is worse than no switch -------------

class _Messages:
    def create(self, **payload):
        self.payload = payload
        return {"content": [{"type": "text", "text": "A note."}],
                "stop_reason": "end_turn"}


class _FakeAnthropic:
    def __init__(self):
        self.messages = _Messages()


@pytest.mark.parametrize("enabled,expects_tool", [(True, True), (False, False)])
def test_search_enabled_decides_whether_a_search_tool_is_sent(monkeypatch,
                                                              enabled, expects_tool):
    monkeypatch.setenv("LQABR_RESEARCH_SEARCH_ENABLED", "1" if enabled else "0")
    client = _FakeAnthropic()
    AnthropicWebSearch(settings=get_settings(refresh=True), client=client,
                       api_key="test-only").research("prompt")
    assert ("tools" in client.messages.payload) is expects_tool


def test_the_note_still_comes_back_with_search_off(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_SEARCH_ENABLED", "0")
    findings = AnthropicWebSearch(settings=get_settings(refresh=True),
                                  client=_FakeAnthropic(),
                                  api_key="test-only").research("prompt")
    assert findings.text == "A note."


# --- the campaign arithmetic the review found untested --------------------

def test_the_campaign_counts_add_up():
    settings = get_settings(refresh=True)
    good, bad = dict(LEAD), dict(UNWRITABLE)
    replies = {"get_blog_summary": BLOG, "upsert_lead_profile": {"status": "updated"}}

    class _ByLead(FakeMCPClient):
        def call_tool(self, name, arguments):
            if name == "get_lead_profile":
                self.calls.append((name, arguments))
                sent = next(iter(arguments.values()))
                return good if sent in ("1", "2") else bad
            return super().call_tool(name, arguments)

    hubspot = HubSpotMCP(client=_ByLead(replies), settings=settings)
    hubspot.list_leads_by_industry = lambda industry, limit=100: ["1", "2", "3"]
    result = run_campaign(CampaignTarget(objectId="T1"), settings=settings,
                          hubspot=hubspot,
                          composer=Composer(provider=FakeSearch(), settings=settings))

    assert (result.leads_found, result.written, result.failed, result.skipped) == (3, 2, 1, 0)
    assert result.status == "partial", "some wrote, some did not"
    assert len(result.results) == 3
    assert [r.objectId for r in result.results if r.status == "failed"] == ["3"]
    assert "bad-data" in next(r for r in result.results if r.status == "failed").error


def test_one_lead_that_explodes_never_stops_the_others():
    settings = get_settings(refresh=True)
    hubspot = HubSpotMCP(client=FakeMCPClient(
        {"get_blog_summary": BLOG, "get_lead_profile": LEAD,
         "upsert_lead_profile": {"status": "updated"}}), settings=settings)
    hubspot.list_leads_by_industry = lambda industry, limit=100: ["1", "2", "3"]

    calls = {"n": 0}
    real = Composer(provider=FakeSearch(), settings=settings)

    class _Flaky(Composer):
        def compose(self, lead, blog):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("the provider exploded")
            return real.compose(lead, blog)

    result = run_campaign(CampaignTarget(objectId="T1"), settings=settings,
                          hubspot=hubspot,
                          composer=_Flaky(provider=FakeSearch(), settings=settings))
    assert result.leads_found == 3 and result.written == 2 and result.failed == 1
    assert result.status == "partial"


# --- from the line-by-line necessity audit, 2026-08-25 ---------------------

def test_a_missing_prompt_file_stops_the_run(monkeypatch):
    """It used to substitute a 141-character fallback for a 1,496-character
    contract, silently, and write the result into live CRM records."""
    import composer as composer_module
    from pathlib import Path
    monkeypatch.setattr(composer_module, "_PROMPT_PATH", Path("/nope/research.md"))
    with pytest.raises(Exception) as caught:
        composer_module.load_system_prompt(get_settings(refresh=True))
    assert "model contract is missing" in str(caught.value)
    assert "research.md" in str(caught.value)


def test_a_missing_prompt_file_fails_the_lead_rather_than_writing_a_bad_note(monkeypatch):
    import composer as composer_module
    from pathlib import Path
    monkeypatch.setattr(composer_module, "_PROMPT_PATH", Path("/nope/research.md"))
    settings = get_settings(refresh=True)
    client = FakeMCPClient({"get_lead_profile": LEAD, "get_blog_summary": BLOG,
                            "upsert_lead_profile": {"status": "updated"}})
    response = run_research(
        ResearchTarget(objectId="1", summary_objectId="T1"), settings=settings,
        hubspot=HubSpotMCP(client=client, settings=settings),
        composer=Composer(provider=FakeSearch(), settings=settings))
    assert response.status == "failed"
    assert "upsert_lead_profile" not in [c[0] for c in client.calls]


def test_the_search_count_reaches_the_wire():
    """`searches` was computed, carried, logged — then dropped at the Composer
    boundary, so every response reported 0."""
    settings = get_settings(refresh=True)
    response = run_research(
        ResearchTarget(objectId="1", summary_objectId="T1"), settings=settings,
        hubspot=HubSpotMCP(client=FakeMCPClient(
            {"get_lead_profile": LEAD, "get_blog_summary": BLOG,
             "upsert_lead_profile": {"status": "updated"}}), settings=settings),
        composer=Composer(provider=FakeSearch(), settings=settings))
    assert response.searches == 2, "FakeSearch reports 2 searches"


def test_one_version_everywhere():
    """client.py sent "0.1" while six literals said "0.1.0" and VERSION,
    which exists for exactly this, was unread."""
    from pathlib import Path
    import research_core
    on_disk = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    assert research_core.__version__ == on_disk
    root = Path(__file__).resolve().parents[1]
    for folder in ("src", "packages"):
        for path in (root / folder).rglob("*.py"):
            if path.name == "__init__.py" and path.parent.name == "research_core":
                continue                      # the file that DEFINES them
            code = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                              if not line.lstrip().startswith("#"))
            assert '"lqabr-research-agent"' not in code, f"{path.name} hard-codes the name"
            assert '"0.1' not in code, f"{path.name} hard-codes a version"
