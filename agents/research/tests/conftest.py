"""Shared fixtures. Every external service is faked — the suite never reaches
the network and needs no credential."""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

from research_core.settings import Settings, get_settings
from research_core.types import BlogFacts, LeadFacts, ResearchFindings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No developer's real .env leaks into a test run."""
    for key in list(os.environ):
        if key.startswith("LQABR_RESEARCH_") or key == "ANTHROPIC_API_KEY":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LQABR_RESEARCH_LOG_FILE", "")
    yield


@pytest.fixture
def settings() -> Settings:
    return get_settings(refresh=True)


@pytest.fixture
def lead() -> LeadFacts:
    return LeadFacts(
        objectId="533963448020", first_name="Mahesh", last_name="Puliganti",
        job_title="President", industry="HEALTHCARE", company="Axiom Law",
        company_about="Alternative legal services provider.",
        company_website="https://www.axiomlaw.com",
        employee_id="E00017", company_id="C0017", decision_maker_flag="Yes",
    )


@pytest.fixture
def blog() -> BlogFacts:
    return BlogFacts(
        blog_published_at="2026-08-27T09:30:00Z",
        blog_summary="Governed AI in regulated workflows needs citations, "
                     "human sign-off and immutable audit trails.",
        blog_industry="HEALTHCARE", ticket_id="329444635358",
    )


class FakeSearch:
    """A SearchProvider that returns fixed findings and records its prompts."""

    name = "fake"

    def __init__(self, text: str = "Grounded note about Axiom Law.",
                 sources: List[str] | None = None, raises: Exception | None = None) -> None:
        self.text = text
        self.sources = sources if sources is not None else ["https://example.com/a"]
        self.raises = raises
        self.prompts: List[str] = []
        self.systems: List[str] = []

    def research(self, prompt: str, *, system: str = "") -> ResearchFindings:
        self.prompts.append(prompt)
        self.systems.append(system)
        if self.raises:
            raise self.raises
        return ResearchFindings(text=self.text, sources=list(self.sources),
                                searches=2, model="fake-model")


class FakeMCPClient:
    """Records tools/call traffic and replays scripted results."""

    def __init__(self, results: Dict[str, Any] | None = None,
                 tools: List[str] | None = None) -> None:
        self.results = results or {}
        self._tools = tools or ["get_lead_profile", "get_blog_summary",
                                "upsert_lead_profile", "upsert_blog_summary"]
        self.calls: List[tuple] = []

    def list_tools(self) -> List[str]:
        return list(self._tools)

    def ensure_ready(self, required=None) -> List[str]:
        return list(self._tools)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        outcome = self.results.get(name)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake_search() -> FakeSearch:
    return FakeSearch()


@pytest.fixture
def fake_mcp_client() -> FakeMCPClient:
    return FakeMCPClient()
