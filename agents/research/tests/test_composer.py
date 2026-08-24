"""The composer: the prompt carries the real facts, the note carries citations."""

from __future__ import annotations

import pytest

from composer import Composer, build_query, load_system_prompt
from research_core.search.base import SearchError
from research_core.settings import get_settings


def test_query_carries_company_industry_and_blog(lead, blog):
    query = build_query(lead, blog)
    assert "Axiom Law" in query
    assert "HEALTHCARE" in query
    assert blog.blog_summary[:30] in query


def test_query_omits_empty_fields(lead, blog):
    lead.company_website = ""
    lead.company_about = ""
    query = build_query(lead, blog)
    assert "Company website" not in query
    assert "About the company" not in query
    assert "None" not in query


def test_system_prompt_has_the_grounding_rule():
    prompt = load_system_prompt(get_settings(refresh=True))
    assert "Ground every claim" in prompt
    assert "{target_words}" not in prompt      # the placeholder was substituted


def test_compose_returns_note_with_sources(lead, blog, fake_search):
    note = Composer(provider=fake_search, settings=get_settings(refresh=True)).compose(lead, blog)
    assert note.text == "Grounded note about Axiom Law."
    assert note.sources == ["https://example.com/a"]


def test_sources_can_be_turned_off(lead, blog, fake_search, monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_INCLUDE_SOURCES", "0")
    note = Composer(provider=fake_search, settings=get_settings(refresh=True)).compose(lead, blog)
    assert note.sources == []


def test_search_failure_propagates(lead, blog):
    from conftest import FakeSearch
    provider = FakeSearch(raises=SearchError("provider down"))
    with pytest.raises(SearchError):
        Composer(provider=provider, settings=get_settings(refresh=True)).compose(lead, blog)


def test_note_appends_sources_for_hubspot(lead, blog, fake_search):
    note = Composer(provider=fake_search, settings=get_settings(refresh=True)).compose(lead, blog)
    text = note.as_hubspot_text()
    assert "Sources: https://example.com/a" in text


def test_note_is_truncated_to_the_cap(lead, blog):
    from conftest import FakeSearch
    provider = FakeSearch(text="x" * 100, sources=[])
    note = Composer(provider=provider, settings=get_settings(refresh=True)).compose(lead, blog)
    assert len(note.as_hubspot_text(max_chars=10)) == 10
