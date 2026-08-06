"""STEP 6 — instruction-based email construction. CHANGED IN V2.1.

Construction is **skill-based and instruction-driven**. A skill is a folder
in here containing a `SKILL.md`: YAML frontmatter naming the industries it
serves, then a body of drafting instructions. The skill is selected from the
lead's industry and fields, and exactly one model call per lead drafts the
email from those instructions.

    skills/
    ├── DRAFTING_RULES.md      shared, prepended to every skill
    ├── technology/SKILL.md
    └── ...

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

NO DEFAULT SKILL. A skill is chosen from the lead's INDUSTRY and nothing
else. A lead whose industry is empty, or which no skill claims, raises
`SkillError` and is flagged unresolved with that reason — it is not sent
industry-neutral copy and it is not silently dropped. Widening coverage is
therefore a matter of adding a skill or of the industry arriving on the
profile, both visible, rather than of a catch-all quietly absorbing leads
nobody wrote copy for.

Adding a skill: `mkdir <name>/` with a `SKILL.md` carrying `name`,
`description` and `industries` frontmatter. No code change —
`load_skills()` discovers it.

Lead facts available to the instructions: first_name, last_name, company,
job_title, industry, location. The email greets the lead by their full name
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
from typing import Any, Callable, Dict, List, Optional, Tuple

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


def select_skill(industry: Optional[str] = None,
                 job_title: Optional[str] = None) -> Tuple[Skill, str]:
    """Pick the skill for this lead from its INDUSTRY. Returns
    ``(skill, reason)``; the reason is what process_log records as WHICH
    SKILL WAS CHOSEN and on what basis.

    Matching is on the lead's industry, exact then substring. There is no
    default skill: a lead with no industry, or an industry no skill claims,
    raises `SkillError` and is flagged unresolved by the caller. Guessing a
    near match or sending industry-neutral copy would both put an
    unapproved email in front of a real person, so neither happens."""
    skills = load_skills()
    normalised = _normalise(industry)

    if not normalised:
        raise SkillError(
            "no industry on the profile — a skill is selected from the industry "
            "and there is no default; lead flagged unresolved")

    for skill in skills.values():
        if normalised in skill.industries:
            return skill, f"industry '{industry}' matched skill '{skill.name}' exactly"

    for skill in skills.values():
        for claimed in skill.industries:
            if claimed and (claimed in normalised or normalised in claimed):
                return skill, f"industry '{industry}' matched skill '{skill.name}' on '{claimed}'"

    raise SkillError(
        f"industry '{industry}' is claimed by no skill "
        f"({', '.join(sorted(skills))}) — lead flagged unresolved")


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


__all__ = ["Skill", "SkillError", "load_skills", "select_skill", "render", "finalise",
           "fill", "build_context", "lead_facts", "SKILL_DIR",
           "SKILL_FILENAME", "RULES_FILENAME", "UNSUBSCRIBE_FOOTER", "RESERVED_SLOTS"]
