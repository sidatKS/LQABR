"""STEP 6 — instruction-based email construction. CHANGED IN V2.1.

OWNERSHIP — THE EMAIL AGENT'S, AND NOTHING ELSE'S
This package lives at `agents/email/skills/` because outreach copy is the email
agent's business logic. It is deliberately NOT in `packages/lqabr_core` (the
only shared code path) and NOT in `mcp/hubspot/` (the shared HubSpot tool),
because it is not shared: no sibling agent reads it, imports it, or drafts from
it, and editing it must never change what text/voice, scheduling, lead_profile,
ingestion or the orchestrator sends.

Three things hold that line, and `tests/test_skills.py` asserts all three:

  1. `SKILL_DIR` is resolved from THIS file's location, so it always points
     inside `agents/email/` and cannot be repointed at a shared directory by
     however the process was launched.
  2. `outreach.py` loads this package by file path under the private module
     name `lqabr_email_skills`, never as a bare `skills` — a bare name would
     collide with any sibling agent that grew a `skills/` package of its own,
     and whichever imported first would silently win.
  3. A repo scan asserts nothing outside `agents/email/` references it.

CLAUDE.md §6 states the convention ("no cross-agent imports: shared code flows
through packages/lqabr_core"). The tests are what make it true.


Construction is **skill-based and instruction-driven**. A skill is a folder
in here containing a `SKILL.md`: YAML frontmatter naming the industries it
serves, then a body of drafting instructions. Exactly one model call per lead
drafts the email from those instructions.

    skills/
    ├── DRAFTING_RULES.md      shared, prepended to the skill
    └── outreach/SKILL.md      the single all-industry instruction set

WHAT CHANGED FROM THE REV 6 DESIGN
Rev 6 specified fill-in-the-blank templates: a fixed subject and HTML body
with `{slot}` markers, where the model only re-worded what was already
approved. That was deliberate — it made the email's claims reviewable in
version control and left the model unable to invent an offer.

This module now hands the model instructions instead, and the model writes
the prose. The offer is therefore constrained by INSTRUCTION rather than by
TEMPLATE, which is a weaker guarantee: a model that ignores its instructions
can now say something nobody approved. Two guardrails survive that change
and are enforced in code, not asked for in prose:

1. `DRAFTING_RULES.md` is prepended to every skill and cannot be overridden
   by a skill file. The "never invent a claim / statistic / customer / URL"
   rules live there once, so they cannot drift between five skill files or
   be dropped from one.
2. `finalise()` appends the unsubscribe footer and substitutes `{cta_url}` /
   `{sender_name}` AFTER the model. A model-written unsubscribe link or CTA
   is discarded, because an invented opt-out link is a compliance problem
   and an invented CTA is a dead one.

There is no longer a template to fall back on, so a model that is
unreachable or unusable raises `SkillError` and the lead is flagged
unresolved at step 5/6 rather than sent an un-personalised email. Failing
loudly is the only safe degradation once the content is model-written.

ONE SKILL, ALL INDUSTRIES — CHANGED IN REV 8 (v4)
Rev 7 had fifteen per-industry skill folders and selected between them on the
lead's industry, with no default: an unmatched industry meant the lead was
flagged and never emailed. That is no longer the shape.

There is now a single `outreach/SKILL.md` covering every sector, because v4
moved the personalisation somewhere else. `lead_context` — the research
agent's knowledge graph for the individual lead — is what makes one email
differ from the next, and it is far more specific than a sector template ever
was. The industry now does exactly one job: it selects which SECTOR RESTRAINT
applies inside the instructions (healthcare says nothing about PHI, financial
services nothing that reads as advice, and so on). All fifteen restraints are
carried in that one file, keyed by industry, plus a strictest-reading default
for a sector that is not listed.

WHAT THIS TRADED AWAY, DELIBERATELY. Rev 7's guarantee was structural: the
copy a lead could receive was fixed in code by its industry. That guarantee is
gone — the model now reads a restraint table and applies the matching row, so
"which restraint binds" is a model decision rather than a selection in code.
What replaced it as the send-safety gate is `lead_context`: a lead with no
research context is not emailed at all (see `outreach.load_context`). An
unrecognised industry is now flagged on process_log rather than blocking the
send — `industry_is_recognised()` is what reports it.

Adding sector coverage is now editing the restraint table and the `industries`
frontmatter in that one file — no new folder, no code change. `select_skill()`
deliberately RAISES if it ever finds more than one skill folder, so a restored
per-industry folder fails loudly instead of silently winning on sort order.

Lead facts available to the instructions: email, first_name, last_name,
company, job_title, industry, industry_group, company_about, company_website,
annual_revenue, lead_context. The email greets the lead by their full name
(`first_name` + `last_name`), falling back to whichever part is present, and
to a plain "Hello," when no name is on record (see DRAFTING_RULES). The
internal `employee_id` is never given to construction and never appears in
the prose.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

SKILL_DIR = Path(__file__).resolve().parent
SKILL_FILENAME = "SKILL.md"
RULES_FILENAME = "DRAFTING_RULES.md"

#: Appended by `finalise()` to every body, after the model. Never model-written.
UNSUBSCRIBE_FOOTER = (
    '<p style="font-size:11px;color:#888;">'
    "You received this because we believe it is relevant to your role. "
    '<a href="%unsubscribe_url%">Unsubscribe</a>.'
    "</p>"
)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_SLOT = re.compile(r"\{([a-z_]+)\}")
#: Slots the model is told to leave in place for code to substitute.
RESERVED_SLOTS = ("cta_url", "sender_name")


class SkillError(RuntimeError):
    """A skill folder that cannot produce an email, or a model that did not.
    Never falls through to sending something unapproved — the caller flags
    the lead as unresolved."""


@dataclass(frozen=True)
class Skill:
    """One set of drafting instructions."""

    name: str
    description: str
    industries: Tuple[str, ...]
    instructions: str
    source_path: str

    def prompt_body(self) -> str:
        """The shared rules followed by this skill's instructions. The rules
        come first so a skill file cannot pre-empt them."""
        return f"{_load_rules()}\n\n---\n\n{self.instructions}"


def _normalise(industry: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (industry or "").strip().lower()).strip()


@lru_cache(maxsize=1)
def _load_rules() -> str:
    path = SKILL_DIR / RULES_FILENAME
    if not path.is_file():
        raise SkillError(
            f"no {RULES_FILENAME} in {SKILL_DIR} — the shared drafting rules are "
            "not optional; every skill inherits them")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise SkillError(f"{RULES_FILENAME} is empty")
    return text


def _parse(path: Path) -> Skill:
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(raw)
    if not match:
        raise SkillError(f"skill {path.parent.name}/{path.name} has no '---' frontmatter block")
    meta: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    instructions = match.group(2).strip()
    if not instructions:
        raise SkillError(f"skill {path.parent.name}/{path.name} carries no drafting instructions")
    industries = tuple(
        _normalise(part) for part in meta.get("industries", "").split(",") if part.strip())
    return Skill(
        name=meta.get("name") or path.parent.name,
        description=meta.get("description", ""),
        industries=industries,
        instructions=instructions,
        source_path=str(path),
    )


@lru_cache(maxsize=1)
def load_skills() -> Dict[str, Skill]:
    """Every skill in this folder, keyed by name. Cached — call
    `load_skills.cache_clear()` after editing a SKILL.md in a dev loop."""
    skills: Dict[str, Skill] = {}
    for path in sorted(SKILL_DIR.glob(f"*/{SKILL_FILENAME}")):
        skill = _parse(path)
        skills[skill.name] = skill
    if not skills:
        raise SkillError(
            f"no */{SKILL_FILENAME} in {SKILL_DIR} — there are no drafting "
            "instructions to select from")
    _load_rules()  # fail at load time, not mid-campaign
    return skills


def match_industry(industry: Optional[str] = None) -> Optional[str]:
    """The claimed industry label this lead's industry resolves to, or None.

    Exact match first, then substring either way, against every industry named
    in skill frontmatter. Returns None for an empty industry and for one that
    is not claimed — the CALLER decides what that means; this reports the fact
    and nothing more."""
    normalised = _normalise(industry)
    if not normalised:
        return None

    skills = load_skills()
    for skill in skills.values():
        if normalised in skill.industries:
            return normalised
    for skill in skills.values():
        for claimed in skill.industries:
            if claimed and (claimed in normalised or normalised in claimed):
                return claimed
    return None


def industry_is_recognised(industry: Optional[str] = None) -> bool:
    """Whether the sector restraint table has an entry for this industry.

    An unrecognised industry no longer stops the send — the instructions carry
    a strictest-reading default for exactly this case — but it IS logged, so an
    operator can see which leads went out un-sectored and widen the frontmatter
    if they should not have."""
    return match_industry(industry) is not None


def select_skill(industry: Optional[str] = None) -> Tuple[Skill, str]:
    """Pick the skill for this lead. Returns ``(skill, reason)``; the reason is
    what process_log records as WHICH SKILL WAS CHOSEN and on what basis.

    NO `job_title` PARAMETER — REMOVED 2026-08-18, deliberately.
    This function accepted one for as long as it existed and never read it,
    while `outreach.construct_email` dutifully passed `profile.job_title` in.
    Every caller therefore read as though the job title refined which copy a
    lead received. It never did: matching was on industry alone.

    It is deleted rather than implemented because rev 8 removed the thing it
    would have refined. Under the fifteen-skill design, narrowing by role
    inside an industry was at least a coherent idea. With one instruction set
    covering every sector there is nothing left to select between, and the job
    title already reaches the model directly as a construction fact — where it
    does real work, shaping the concern the email speaks to. Selecting on it
    here would be a second, weaker copy of that.

    CHANGED FOR REV 8. There is now ONE set of drafting instructions covering
    every sector, and the industry no longer selects it. `lead_context` is what
    personalises the email, and the gate deciding whether a lead is safe to
    email is that context being present — not its industry matching a folder
    name. See `outreach.load_context`.

    The industry still does one job: it selects which sector restraint applies
    INSIDE the instructions. So it is classified here and named in the reason —

        recognised   the restraint table has an entry for it
        unknown      no entry; the instructions' strictest-reading default
                     applies and the caller logs a named warning
        absent       no industry on the record at all; same default

    — and in none of those cases is the lead dropped. That is the deliberate
    change from rev 7, where an unmatched industry meant no send at all.

    Still raises `SkillError` when there is nothing to draft from, or when the
    single-skill layout has been broken: those are deployment faults, not
    lead-level ones, and must not be resolved by guessing."""
    skills = load_skills()
    if len(skills) != 1:
        # Rev 8 is a single instruction set. More than one folder means a
        # retired per-industry skill was restored or a new one added — say so
        # rather than silently drafting from whichever sorted first.
        raise SkillError(
            f"expected exactly one skill covering every industry, found "
            f"{len(skills)} ({', '.join(sorted(skills))}). Rev 8 replaced the "
            "per-industry skills with a single instruction set — remove the "
            "extra folders, or restore per-industry selection deliberately.")
    skill = next(iter(skills.values()))

    matched = match_industry(industry)
    if matched:
        return skill, (f"industry '{industry}' matched the '{matched}' sector "
                       f"restraint in skill '{skill.name}'")
    if not _normalise(industry):
        return skill, (f"no industry on the profile — skill '{skill.name}' applies its "
                       "strictest-reading default restraint")
    return skill, (f"industry '{industry}' has no sector restraint entry — skill "
                   f"'{skill.name}' applies its strictest-reading default restraint")


def fill(template: str, context: Dict[str, Any]) -> str:
    """Substitute the known slots only.

    Plain replacement rather than ``str.format``: a model-written line
    containing literal braces would make ``format`` raise KeyError, and a
    send must never fail over punctuation."""
    for key, value in context.items():
        template = template.replace("{" + key + "}", str(value if value is not None else ""))
    return template


def build_context(profile_context: Dict[str, Any],
                  cta_url: Optional[str] = None,
                  sender_name: Optional[str] = None) -> Dict[str, Any]:
    """The full substitution context: the lead's real fields plus the
    configured sender/CTA. Nothing here is invented — a field the profile
    does not carry stays empty and the instructions tell the model to write
    around it."""
    context = dict(profile_context)
    context["cta_url"] = cta_url or os.environ.get("LQABR_CTA_URL", "")
    context["sender_name"] = sender_name or os.environ.get("LQABR_SENDER_NAME", "The LQABR Team")
    return context


def lead_facts(context: Dict[str, Any]) -> Dict[str, Any]:
    """The lead facts the model is allowed to use — the profile fields only,
    with the reserved markers and anything empty stripped out. The model
    never receives `cta_url` or `sender_name` as a value it could inline."""
    return {k: v for k, v in context.items()
            if k not in RESERVED_SLOTS and v not in (None, "")}


def finalise(subject: str, body: str, context: Dict[str, Any]) -> Tuple[str, str]:
    """Post-process model output. Enforced in code, not asked for in prose.

    Substitutes the reserved markers with real values, strips any
    unsubscribe markup the model wrote for itself, and appends the one
    compliant footer. A model-invented opt-out link never ships."""
    subject = fill(subject, context).strip()
    body = fill(body, context).strip()

    # Discard any opt-out the model wrote; ours is the only one that ships.
    body = re.sub(r"<p[^>]*>(?:(?!</p>).)*?unsubscrib(?:e|ing)(?:(?!</p>).)*?</p>",
                  "", body, flags=re.IGNORECASE | re.DOTALL)
    body = body.replace("%unsubscribe_url%", "").strip()

    return subject, f"{body}\n{UNSUBSCRIBE_FOOTER}"


def render(skill: Skill, context: Dict[str, Any],
           model_fn: Optional[Callable[[str, Dict[str, Any]], Tuple[str, str]]] = None
           ) -> Tuple[str, str, bool]:
    """Draft the email for one lead from the selected skill's instructions.

    ``model_fn(prompt_body, lead_facts) -> (subject, html_body)`` is the
    single model call per lead.

    Returns ``(subject, html_body, used_model)``. Unlike the rev 6 template
    build there is NO deterministic fallback: the content is model-written,
    so an unreachable or unusable model raises `SkillError` and the caller
    flags the lead rather than sending something unapproved."""
    if model_fn is None:
        raise SkillError(
            f"skill '{skill.name}' is instruction-based and needs a model to draft it; "
            "no model_fn was supplied")

    drafted_subject, drafted_body = model_fn(skill.prompt_body(), lead_facts(context))
    if not drafted_subject or not drafted_body:
        raise SkillError(
            f"skill '{skill.name}': the model returned no usable subject/body")

    subject, body = finalise(drafted_subject, drafted_body, context)
    if not subject or not body:
        raise SkillError(f"skill '{skill.name}': draft was empty after post-processing")
    return subject, body, True


__all__ = ["Skill", "SkillError", "load_skills", "select_skill", "match_industry",
           "industry_is_recognised", "render", "finalise",
           "fill", "build_context", "lead_facts", "SKILL_DIR",
           "SKILL_FILENAME", "RULES_FILENAME", "UNSUBSCRIBE_FOOTER", "RESERVED_SLOTS"]
