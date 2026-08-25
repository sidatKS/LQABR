"""The composer: the prompt carries the real facts, the note carries citations."""

from __future__ import annotations

import pytest

from composer import (Composer, build_query, load_system_prompt,
                      strip_preamble)
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


# --- the note must be the note, not the model talking about writing it -------
# Both shapes below came off a real run (2026-08-24, FINANCIAL_SERVICES):
# tool-use narration between searches, and a research recap followed by an
# announcement line. Neither belongs on a HubSpot contact.

NOTE = ("Brex is now a wholly owned subsidiary of Capital One after a $5.15 "
        "billion acquisition, and its core identity is an AI-native finance "
        "platform combining corporate cards, spend management and banking.")


def test_announcement_line_and_rule_are_dropped():
    raw = ("Brex is a modern AI-native platform. The search did not surface a "
           "President by that name.\n\n"
           "Here is the `lead_context` note:\n\n---\n\n") + NOTE
    assert strip_preamble(raw) == NOTE


def test_the_recap_before_the_announcement_goes_too():
    raw = "A recap paragraph about the search.\n\nHere is the note:\n\n" + NOTE
    assert "recap paragraph" not in strip_preamble(raw)


def test_a_clean_note_is_returned_untouched():
    assert strip_preamble(NOTE) == NOTE


def test_here_is_mid_sentence_is_not_a_marker():
    """Only a colon at END OF LINE announces a note — prose keeps its words."""
    raw = ("MoneyLion's pitch is simple: here is your whole financial life in "
           "one app. That framing is exactly what a governed-AI story respects.")
    assert strip_preamble(raw) == raw


def test_nothing_is_dropped_when_too_little_would_survive():
    """A short tail means the marker was misread — keep the original."""
    raw = "Here is the note:\n\nToo short."
    assert strip_preamble(raw) == raw.strip()
