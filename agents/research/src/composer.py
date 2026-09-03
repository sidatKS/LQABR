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
    from research_core.research_logging import ResearchLogging, get_obs
    from research_core.settings import Settings, get_settings
    from research_core.search.base import SearchError, SearchProvider, build_provider
    from research_core.types import BlogFacts, LeadFacts, ResearchNote
except ImportError:  # pragma: no cover - direct `uvicorn service_app:app`
    from ..packages.research_core.research_logging import ResearchLogging, get_obs  # type: ignore
    from ..packages.research_core.settings import Settings, get_settings  # type: ignore
    from ..packages.research_core.search.base import (  # type: ignore
        SearchError, SearchProvider, build_provider)
    from ..packages.research_core.types import (  # type: ignore
        BlogFacts, LeadFacts, ResearchNote)

# A line that announces the note rather than being it: "Here is the
# lead_context note:", "Below is the note for Brex:". Matched at any line
# start, not just the first — the model often writes a research recap FIRST
# and only then announces the note, so the opener is mid-text.
#: The model announcing the note rather than writing it. Built from every
#: opener seen in production (15 of them, across four campaigns on 2026-08-25),
#: which is why it is this permissive:
#:
#:   "Here is the `lead_context` note:"
#:   "⚠️ **Important note before the lead_context note:**"
#:   "**⚠️ Editorial note before the lead_context note:**"   glyph INSIDE the bold
#:   "⚠️ **Important flag before writing the note:**"        flag, not note
#:   "**Note to writer:**"
#:   "I now have sufficient information… Let me note upfront:"
#:
#: `_JUNK` is the run of markdown, emoji and punctuation any of them may open
#: with, in any order — matching `**` before `⚠️` and after it.
_JUNK = r"[^A-Za-z0-9\n]{0,12}"
_OPENERS = (
    r"here(?:'s| is)\b|below is\b|i'?ll\b|i(?: now)? have\b|i'?ve\b"
    r"|let me\b|now let me\b|i'?m going to\b|sure[,!.]|certainly[,!.]"
    r"|(?:an? )?(?:important|quick|brief|editorial|final|one)?[ \t]*"
    r"(?:note|flag|caveat|warning|disclaimer|heads[- ]up)\b"
    # The model naming the PROPERTY as the heading over the real note:
    #   "**`lead_context` note:**"        (Innovaccer, 2026-09-01)
    # Seen after a recap whose own opener ended in a full stop, so nothing
    # earlier in the text anchored — this line was the only marker present.
    r"|(?:the[ \t]+)?lead_context\b"
)

#: The announcement as its own line, ending in a colon — what `strip_preamble`
#: splits on, keeping only what follows the LAST one.
_PREAMBLE = re.compile(
    rf"^[ \t]*{_JUNK}(?:{_OPENERS})[^\n]{{0,200}}?:\**[ \t]*$",
    re.IGNORECASE | re.MULTILINE)

# A markdown rule the model puts between the announcement and the note.
_LEADING_RULE = re.compile(r"\A(?:\s*(?:-{3,}|\*{3,}|_{3,})\s*\n)+")

#: The model HEADING the real note with the property's own name. Seen twice in
#: one afternoon, punctuated differently each time, which is why this matches on
#: the shape rather than the punctuation:
#:
#:   "**`lead_context` note:**"    Innovaccer, 2026-09-01 11:04   colon
#:   "### `lead_context` note"     Accolade,   2026-09-01 16:39   no colon
#:
#: A SHORT standalone line naming the property is a heading, never prose — a
#: real note discusses the company, it does not announce itself. Requiring the
#: whole line to be little more than the property name is what keeps this from
#: matching a sentence that merely mentions `lead_context`.
_NOTE_HEADING = re.compile(
    r"^[^A-Za-z0-9\n]{0,12}(?:the[ \t]+)?[`'\"*_]*lead[-_ ]?context[`'\"*_]*"
    r"[ \t]*(?:note|entry|section|itself)?[ \t]*:?[^A-Za-z0-9\n]{0,8}$",
    re.IGNORECASE | re.MULTILINE)

#: The same announcement written INLINE, so the commentary continues on the
#: line rather than after it. The colon is mid-line, so `_PREAMBLE` (anchored on
#: end-of-line) cannot see it — the whole PARAGRAPH is the aside.
_ASIDE = re.compile(rf"\A[ \t]*{_JUNK}(?:{_OPENERS})[^\n]{{0,200}}?:",
                    re.IGNORECASE)


def _drop_leading_asides(text: str) -> str:
    """Whole paragraphs the model addressed to us rather than to the record."""
    paragraphs = re.split(r"\n\s*\n", text)
    while len(paragraphs) > 1 and _ASIDE.match(paragraphs[0]):
        paragraphs = paragraphs[1:]
    return "\n\n".join(paragraphs).strip()


def strip_preamble(text: str) -> str:
    """Keep only the note, dropping anything the model said about writing it.

    Takes what follows the LAST "here is the note:" style line, because a
    recap paragraph before that line is commentary, not context. Guarded: the
    opener must end in a colon at end-of-line, and at least 80 characters must
    survive — otherwise the original is returned untouched, so a note that
    merely contains such a phrase is never truncated.
    """
    tail = text
    # Either shape counts as the announcement; the LAST one wins, so a recap
    # and its sources are dropped even when both appear.
    matches = list(_PREAMBLE.finditer(text)) + list(_NOTE_HEADING.finditer(text))
    if matches:
        tail = text[max(m.end() for m in matches):]
    tail = _LEADING_RULE.sub("", tail.lstrip("\n")).strip()
    tail = _LEADING_RULE.sub("", _drop_leading_asides(tail)).strip()
    return tail if len(tail) >= 80 else text.strip()


_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "research.md"

#: The prompt deliberately never names the CRM field the note is stored in.
#: While it did, the model kept heading its own output with that name and the
#: heading had to be stripped back off afterwards — twice in one afternoon,
#: punctuated differently each time (`**...note:**` at 11:04, `### ...note` at
#: 16:39). Telling it the storage field bought nothing: the model needs to know
#: what the note is FOR, not where it lands. `strip_preamble` stays as the net,
#: but this is why the net should rarely have to catch anything.
#: If you reintroduce the field name here, expect the headings back.


def load_system_prompt(settings: Settings) -> str:
    """The prompt file is the contract with the model — editing copy is a file
    change, not a code change.

    It RAISES when the file is missing. There used to be a 141-character
    fallback here against a 1,496-character contract, substituted silently: a
    deploy that shipped without `prompts/research.md` would write notes from
    two sentences into live CRM records and report `completed` on every one.
    A broken deployment should fail, loudly, on the first lead.
    """
    try:
        text = _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise SearchError(
            f"the model contract is missing: {_PROMPT_PATH} could not be read "
            f"({exc.strerror}). This file IS the prompt — the agent will not "
            "research from a substitute.") from exc
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
                 obs: ResearchLogging | None = None) -> None:
        self._settings = settings or get_settings()
        self._obs = obs or get_obs()
        self._provider = provider or build_provider(self._settings, obs=self._obs)

    def compose(self, lead: LeadFacts, blog: BlogFacts) -> ResearchNote:
        """Raises SearchError when the research pass fails — the caller reports
        the failure with the step named. It never returns an ungrounded note."""
        settings = self._settings
        prompt = build_query(lead, blog)
        system = load_system_prompt(settings)

        findings = self._provider.research(prompt, system=system)
        cleaned = strip_preamble(findings.text)
        if len(cleaned) != len(findings.text):
            # The model narrated before writing. Say how much was dropped —
            # a preamble stripper that starts eating the note must be visible.
            self._obs.process.emit("compose_preamble_stripped",
                                   objectId=lead.objectId,
                                   raw_chars=len(findings.text),
                                   kept_chars=len(cleaned),
                                   dropped_chars=len(findings.text) - len(cleaned))
        note = ResearchNote(
            text=cleaned,
            sources=list(findings.sources),
            searches=findings.searches,
        )
        return note


__all__ = ["Composer", "build_query", "load_system_prompt",
           "strip_preamble", "SearchError"]
