"""Turn (lead + published post) into a grounded lead_context note.

Deterministic prompt assembly here; the model call itself lives behind the
SearchProvider so it stays mockable. This module owns WHAT is asked and how the
answer is bounded — never how the vendor is reached.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

try:
    from research_core.obs import Observability, get_obs
    from research_core.settings import Settings, get_settings
    from research_core.search.base import SearchError, SearchProvider, build_provider
    from research_core.types import BlogFacts, LeadFacts, ResearchNote
except ImportError:  # pragma: no cover - direct `uvicorn service_app:app`
    from ..packages.research_core.obs import Observability, get_obs  # type: ignore
    from ..packages.research_core.settings import Settings, get_settings  # type: ignore
    from ..packages.research_core.search.base import (  # type: ignore
        SearchError, SearchProvider, build_provider)
    from ..packages.research_core.types import (  # type: ignore
        BlogFacts, LeadFacts, ResearchNote)

# A line that announces the note rather than being it: "Here is the
# lead_context note:", "Below is the note for Brex:". Matched at any line
# start, not just the first — the model often writes a research recap FIRST
# and only then announces the note, so the opener is mid-text.
_PREAMBLE = re.compile(
    r"^[ \t]*(?:here(?:'s| is)\b|below is\b|i'?ll\b|i have\b|i've\b"
    r"|let me\b|now let me\b|i'?m going to\b|sure[,!.]|certainly[,!.])"
    r"[^\n]{0,200}?:[ \t]*$",
    re.IGNORECASE | re.MULTILINE)

# A markdown rule the model puts between the announcement and the note.
_LEADING_RULE = re.compile(r"\A(?:\s*(?:-{3,}|\*{3,}|_{3,})\s*\n)+")


def strip_preamble(text: str) -> str:
    """Keep only the note, dropping anything the model said about writing it.

    Takes what follows the LAST "here is the note:" style line, because a
    recap paragraph before that line is commentary, not context. Guarded: the
    opener must end in a colon at end-of-line, and at least 80 characters must
    survive — otherwise the original is returned untouched, so a note that
    merely contains such a phrase is never truncated.
    """
    tail = text
    matches = list(_PREAMBLE.finditer(text))
    if matches:
        tail = text[matches[-1].end():]
    tail = _LEADING_RULE.sub("", tail.lstrip("\n")).strip()
    return tail if len(tail) >= 80 else text.strip()


_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "research.md"

_FALLBACK_SYSTEM = (
    "You are a B2B research assistant. Ground every claim in a search result; "
    "never invent facts about a company. Write one plain-prose paragraph."
)


def load_system_prompt(settings: Settings) -> str:
    """The prompt file is the contract with the model — editing copy is a file
    change, not a code change."""
    try:
        text = _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_SYSTEM
    return text.replace("{target_words}", str(settings.note_target_words))


def build_query(lead: LeadFacts, blog: BlogFacts) -> str:
    """What the model is asked to research. Only facts we actually hold — an
    empty field is omitted rather than sent as the string 'None'."""
    lines = ["Research this lead and write the lead_context note.", "", "## The lead"]
    for label, value in (
        ("Name", lead.display_name),
        ("Job title", lead.job_title),
        ("Company", lead.company),
        ("Industry", lead.industry),
        ("Company website", lead.company_website),
        ("About the company", lead.company_about),
    ):
        if value:
            lines.append(f"- {label}: {value}")

    lines += ["", "## The post we published"]
    if blog.blog_industry:
        lines.append(f"- Industry: {blog.blog_industry}")
    if blog.blog_published_at:
        lines.append(f"- Published: {blog.blog_published_at}")
    lines += ["- Summary:", blog.blog_summary.strip()]

    industry = lead.industry or blog.blog_industry or "target"
    lines += ["", "## Your task"]
    if lead.company:
        lines.append(f"Search the web for what is true right now about "
                     f"{lead.company} and the {industry} industry, focused on "
                     "the themes above. Then write the note.")
    else:
        # No company NAME on the record — only an internal id, which is not
        # searchable. Say so rather than letting the model search for a code:
        # an id like "C0017" matches unrelated things on the open web and the
        # note comes back grounded in the wrong company entirely.
        lines.append(f"The company NAME is not on this record, so do NOT try to "
                     f"identify the company and do not search for any identifier. "
                     f"Research the {industry} industry against the themes above "
                     f"and write the note at the industry level, addressed to a "
                     f"{lead.job_title or 'senior leader'} in that industry. "
                     "Open by stating plainly that the company could not be "
                     "identified from the record.")
    return "\n".join(lines)


class Composer:
    """Assembles the prompt, runs the grounded pass, returns the note."""

    def __init__(self, provider: Optional[SearchProvider] = None, *,
                 settings: Settings | None = None,
                 obs: Observability | None = None) -> None:
        self._settings = settings or get_settings()
        self._obs = obs or get_obs()
        self._provider = provider or build_provider(self._settings, obs=self._obs)

    def compose(self, lead: LeadFacts, blog: BlogFacts) -> ResearchNote:
        """Raises SearchError when the research pass fails — the caller reports
        the failure with the step named. It never returns an ungrounded note."""
        settings = self._settings
        prompt = build_query(lead, blog)
        system = load_system_prompt(settings)

        self._obs.process.emit("compose_start", object_id=lead.object_id,
                               company=lead.company, industry=lead.industry,
                               blog_chars=len(blog.blog_summary),
                               search_enabled=settings.search_enabled)

        findings = self._provider.research(prompt, system=system)
        note = ResearchNote(
            text=strip_preamble(findings.text),
            sources=list(findings.sources) if settings.include_sources else [],
        )
        self._obs.process.emit("compose_ok", object_id=lead.object_id,
                               chars=len(note.text), sources=len(note.sources),
                               searches=findings.searches)
        return note


__all__ = ["Composer", "build_query", "load_system_prompt",
           "strip_preamble", "SearchError"]
