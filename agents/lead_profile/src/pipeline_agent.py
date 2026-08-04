"""The deterministic orchestrator — the DEFAULT. No model, no tokens, no key.

DECIDED 31 Jul (Stephen): this agent does not need an LLM. The pipeline
(load -> build -> push) has zero decision points, so a model added only cost:
a second external dependency (an Anthropic billing outage stopped the pipeline
on 31 Jul), per-run token spend, latency, and a §7.4 violation pending
sign-off. §7.4 ("Deterministic — NO model") is hereby RESTORED for the default
path.

This is still ONE AGENTIC RUN: a ``BaseAgent`` subclass is a first-class ADK
agent — ``adk run``, ``adk web``, ``InMemoryRunner`` and the Cloud Run Job all
drive it identically. "Agentic" and "LLM" are independent axes in ADK.

The LlmAgent variant still exists (agents/lead_profile/src/llm_agent.py) as
an OPT-IN operator console — set LQABR_ORCHESTRATOR=llm. Same tools, same
pipeline underneath; the model only ever narrated, so nothing functional is
lost by defaulting it off.

Operator input is plain English parsed by a WRITTEN GRAMMAR — generous with
synonyms and word order, strict about meaning, documented in USAGE:
    push/send/sync/write/upload ... [to hubspot]        full run
    "... first/limit/only N"                            cap the push
    "... company C123 and C9" / "employees E1 E2"       selective push
    "... <industry> leads" / "industry retail"          selective push
    "how many <industry> leads ..." / "count ..."       count only, no push
    "dry run"                                           Steps 1-3, no HubSpot
    "lookup/show/get E1042" / "lookup email a@b.c"      Step 6 read path
Anything the grammar does not match gets usage help — never a guess, never a
widened selection. Understanding arbitrary unrestricted English is what the
opt-in LLM console is for; a deterministic parser can only be generous, not
generative.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from lqabr_core.obs import Observability, RunContext, get_obs, set_current_run, set_obs
from lqabr_core.leadgen.hubspot.crm import get_lead_profile, record_unresolved
from lqabr_core.leadgen.hubspot.failures import SystemicFailure

# Co-located modules; support both package (ADK) and flat (tests) import styles.
try:
    from .tools import filter_profiles
    from .build_profile import build_profiles
    from .call_mcp import push_profiles
    from .load_csv import CsvStructureError, default_incoming_dir, load_csvs
except ImportError:  # pragma: no cover
    from tools import filter_profiles  # type: ignore
    from build_profile import build_profiles  # type: ignore
    from call_mcp import push_profiles  # type: ignore
    from load_csv import CsvStructureError, default_incoming_dir, load_csvs  # type: ignore

USAGE = (
    "I run the lead-profile pipeline deterministically (no model). I understand\n"
    "plain sentences built from these patterns — anything else gets this help,\n"
    "never a guess:\n"
    "\n"
    "  PUSH   push/send/sync/write/upload/insert/run ... to hubspot\n"
    "         'run the pipeline' · 'push all records' · 'send the leads to hubspot'\n"
    "  SOME   add a selection:\n"
    "         'push only the first 5' · 'push company C123 and C9'\n"
    "         'push employees E1042, E1077' · 'push the manufacturing leads'\n"
    "  COUNT  'how many retail leads do we have?' · 'count company C1'\n"
    "  DRY    'dry run' — build and report, no HubSpot calls\n"
    "  READ   'lookup E1042' · 'show E1042' · 'what about email a@b.com'\n"
    "\n"
    "Ids are recognised after the words employee(s)/lead(s) or company/companies\n"
    "(tokens containing a digit). Industries as 'industry retail' or '<name> leads'."
)

# --- the written grammar -----------------------------------------------------
# Deterministic parsing means: generous with SYNONYMS and word order, strict
# about MEANING. Every pattern below is documented in USAGE; anything that
# matches none of them returns help. The parser never guesses an id and never
# widens a selection.

_RUN_VERBS = ("push", "send", "sync", "upload", "insert", "write", "run", "start", "pipeline", "dry")
_COUNT_RE = re.compile(r"\bhow\s+many\b|\bcount\b", re.IGNORECASE)
_LOOKUP_RE = re.compile(
    r"\b(?:look\s*up|show|read|get|fetch|check(?:\s+(?:on|the\s+status\s+of|status\s+of))?"
    r"|status\s+of|what\s+about)\s+"
    r"(?:the\s+)?(?:lead\s+|employee\s+|record\s+)?(email\s+)?([\w.@+-]+)",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(r"\b(?:limit|first|top|only)\s+(\d+)\b", re.IGNORECASE)
_BARE_QTY_RE = re.compile(r"\b(\d+)\s+(?:leads|records|profiles|contacts)\b", re.IGNORECASE)
_DIR_RE = re.compile(r"\b(?:from|dir|directory)\s+(\S+)", re.IGNORECASE)
# id lists: tokens containing a digit, separated by , / and / or / & / space
_ID_LIST = r"((?:[\w-]*\d[\w-]*)(?:\s*(?:,|and|or|&|\s)\s*[\w-]*\d[\w-]*)*)"
_EMPLOYEE_KEY_RE = re.compile(r"\b(?:employees?|leads?)\s+" + _ID_LIST, re.IGNORECASE)
_COMPANY_KEY_RE = re.compile(r"\b(?:compan(?:y|ies))\s+" + _ID_LIST, re.IGNORECASE)
_EMPLOYEE_WORD_RE = re.compile(r"\b(?:employees?)\b", re.IGNORECASE)
_COMPANY_WORD_RE = re.compile(r"\b(?:compan(?:y|ies))\b", re.IGNORECASE)
# industry: 1-3 words (allowing "oil & gas") before leads/records/…, or "industry X"
_INDUSTRY_RE = re.compile(
    r"\bindustry\s+([A-Za-z][\w-]*(?:\s*&\s*[A-Za-z][\w-]*)?(?:\s+[A-Za-z][\w-]*)?)"
    r"|\b([A-Za-z][\w-]*(?:\s*&\s*[A-Za-z][\w-]*)?(?:\s+[A-Za-z][\w-]*)?)"
    r"\s+(?:leads|records|companies|profiles)\b",
    re.IGNORECASE,
)
_NOT_INDUSTRY = {
    "the", "all", "my", "our", "these", "those", "this", "that", "first", "new",
    "remaining", "of", "to", "some", "few", "more", "every", "any", "many",
    "how", "which", "what", "for", "with", "from", "have", "has",
    "matching", "selected", "qualified", "decision", "maker", "please", "pls",
    "kindly", "now", "again", "upload", "count", *_RUN_VERBS,
}
# A deterministic grammar cannot negate a selection. Refuse rather than invert.
_NEGATION_RE = re.compile(
    r"\bexcept\b|\bexcluding\b|\bexclude\b|\bwithout\b|\bother\s+than\b"
    r"|\bapart\s+from\b|\bbesides\b|\bskip(?:ping)?\b|\bdon'?t\b|\bdo\s+not\b"
    r"|\bnever\b|\bexcept\s+for\b",
    re.IGNORECASE,
)
# An UNSCOPED run needs an explicit everything-marker; a stray verb in prose
# ("how do i run this") must never fire 263 CRM writes.
_ALL_RE = re.compile(
    r"\ball\b|\beverything\b|\bevery\b|\bentire\b|\bfull\b|\bwhole\b"
    r"|\bcomplete\b|\bpipeline\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"^(?:how|what|why|when|where|who|which|is|are|was|were|does|do|did|has|have|had|am|should)\b",
    re.IGNORECASE,
)


def _ids(raw: str) -> str:
    """Normalise a captured id list ('C123, C9 and C77') to 'C123,C9,C77'."""
    return ",".join(_ID_TOKEN_RE.findall(raw or ""))
_ID_TOKEN_RE = re.compile(r"[\w-]*\d[\w-]*")


def _refuse(message: str) -> dict[str, Any]:
    return {"action": "refuse", "message": message}


def parse_command(text: str) -> dict[str, Any]:
    """Deterministic operator-intent parsing.

    Contract: WINS are documented patterns, generously matched (synonyms, word
    order, politeness, punctuation, case). Everything else FAILS SAFE — help or
    an explicit refusal. There is no path from unrecognised input to a write.
    """
    text = (text or "").strip()
    lowered = text.lower()

    is_count = bool(_COUNT_RE.search(lowered))
    has_run_verb = any(re.search(rf"\b{verb}\b", lowered) for verb in _RUN_VERBS)

    # Negation cannot be expressed in this grammar — inverting or ignoring it
    # would push the OPPOSITE of what the operator meant. Refuse, explain.
    if (has_run_verb or is_count) and _NEGATION_RE.search(lowered):
        return _refuse(
            "I can't apply exclusions ('except', 'skip', 'don't', 'excluding') — "
            "a written grammar can only select, not negate. Name what you DO "
            "want: e.g. 'push company C2 and C3' or 'push the retail leads'."
        )

    # READ one lead — only when it is not a push/count sentence.
    if not is_count and not has_run_verb:
        for match in _LOOKUP_RE.finditer(text):
            key = match.group(2).strip().rstrip("?.!,")
            by_email = bool(match.group(1)) or "@" in key
            if key and (by_email or any(ch.isdigit() for ch in key)):
                return {"action": "lookup", ("email" if by_email else "employee_id"): key}

    if not (is_count or has_run_verb):
        return {"action": "help"}

    command: dict[str, Any] = {"action": "count" if is_count else "run"}

    if match := _LIMIT_RE.search(text):
        command["limit"] = int(match.group(1))
    elif match := _BARE_QTY_RE.search(text):
        command["limit"] = int(match.group(1))  # "push 50 leads"
    if command["action"] == "run" and "dry" in lowered:
        command["dry_run"] = True
    if match := _DIR_RE.search(text):
        command["incoming_dir"] = match.group(1)
    if match := _EMPLOYEE_KEY_RE.search(text):
        command["employee_ids"] = _ids(match.group(1))
    if match := _COMPANY_KEY_RE.search(text):
        command["company_ids"] = _ids(match.group(1))
    if match := _INDUSTRY_RE.search(text):
        words = (match.group(1) or match.group(2) or "").split()
        while words and words[0].lower() in _NOT_INDUSTRY:
            words.pop(0)
        cleaned = " ".join(words)
        if cleaned and not any(ch.isdigit() for ch in cleaned):
            command["industry"] = cleaned

    # "push company Acme" — a selection keyword with no recognisable id must
    # never degrade into an unfiltered full push.
    if (
        "company_ids" not in command
        and _COMPANY_WORD_RE.search(lowered)
        and not command.get("industry")
    ):
        return _refuse(
            "You named a company but I couldn't recognise an id (ids contain a "
            "digit, e.g. C123). I won't fall back to pushing everything — "
            "please give the Company_ID(s)."
        )
    if "employee_ids" not in command and _EMPLOYEE_WORD_RE.search(lowered) and not (
        command.get("industry") or "leads" in lowered
    ):
        return _refuse(
            "You named an employee but I couldn't recognise an id (ids contain "
            "a digit, e.g. E1042). I won't fall back to pushing everything — "
            "please give the Employee_ID(s)."
        )

    # An unscoped RUN needs explicit intent. A verb inside a question or prose
    # ("how do i run this", "the pipeline failed, why?") never fires a push.
    if command["action"] == "run":
        scoped = any(
            k in command for k in ("limit", "employee_ids", "company_ids", "industry")
        ) or command.get("dry_run")
        # A question is never an unscoped write order — whether it starts
        # interrogatively or merely ends in '?'.
        if not scoped and (_QUESTION_RE.match(lowered) or lowered.rstrip().endswith("?")):
            return {"action": "help"}
        if not scoped and not _ALL_RE.search(lowered):
            return _refuse(
                "That sounds like a push, but with no scope. Say 'run the "
                "pipeline' / 'push all records' for everything, or name a "
                "selection: 'push company C123', 'push the retail leads', "
                "'push the first 5'."
            )
    return command


class DeterministicLeadProfileAgent(BaseAgent):
    """Steps 1-5 as one agentic run. Every branch below is a written rule."""

    incoming_dir: str | None = None
    default_limit: int | None = None

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        run_ctx = RunContext(
            run_id=str(ctx.invocation_id), agent=self.name, uses_model=False
        )
        set_current_run(run_ctx)
        obs = set_obs(Observability(run_ctx))
        obs.system_snapshot("container_up", agent=self.name, invocation="adk")
        obs.process.emit(
            "agent_invoked",
            step=0,
            orchestration="deterministic",
            note="no model; step order is code, operator text parsed by written rules",
        )

        text = ""
        if ctx.user_content and ctx.user_content.parts:
            text = "".join(part.text or "" for part in ctx.user_content.parts)
        command = parse_command(text)
        obs.process.emit("operator_command", raw=text[:200], parsed=command)

        try:
            if command["action"] == "help":
                yield self._say(ctx, USAGE)
            elif command["action"] == "refuse":
                yield self._say(ctx, command["message"])
            elif command["action"] == "lookup":
                async for event in self._do_lookup(ctx, command):
                    yield event
            elif command["action"] == "count":
                async for event in self._do_count(ctx, command):
                    yield event
            else:
                async for event in self._do_run(ctx, command):
                    yield event
        finally:
            obs.process.emit("agent_complete")
            obs.system_snapshot("container_down")

    # -- the pipeline ---------------------------------------------------------

    async def _do_run(self, ctx: InvocationContext, command: dict[str, Any]):
        obs = get_obs()

        base = Path(
            command.get("incoming_dir") or self.incoming_dir or default_incoming_dir()
        )

        # STEP 1+2 — structural failure halts, naming file + column.
        try:
            feed = await asyncio.to_thread(load_csvs, base)
        except CsvStructureError as exc:
            yield self._say(
                ctx,
                f"HALTED — structural feed failure: {exc}\n"
                "No lead was pushed. Fix the feed and run again.",
                state={"feed_loaded": False},
            )
            return
        yield self._say(
            ctx,
            f"Step 1+2 — loaded: {feed.counts}",
            state={"feed_loaded": True, "feed_counts": feed.counts},
        )

        # STEP 3 — build in memory; unresolved persisted, never dropped.
        build = await asyncio.to_thread(build_profiles, feed)
        await asyncio.to_thread(
            record_unresolved, [lead.to_dict() for lead in build.unresolved]
        )
        yield self._say(
            ctx,
            f"Step 3 — built: {build.summary}",
            state={
                "profiles_built": len(build.profiles),
                "unresolved": len(build.unresolved),
            },
        )

        if command.get("dry_run"):
            obs.process.emit("dry_run", step=4, skipped="push_lead_profiles")
            yield self._say(
                ctx,
                f"Dry run — stopping before Step 4. {len(build.profiles)} profiles "
                f"ready, {len(build.unresolved)} unresolved (errors/unresolved.jsonl). "
                "No HubSpot calls made, no token minted.",
            )
            return

        # Selection — the operator can push a subset; the filter is written
        # rules over the built profiles, logged, and a 0-match NEVER widens.
        profiles, spec = filter_profiles(
            build.profiles,
            employee_ids=command.get("employee_ids", ""),
            company_ids=command.get("company_ids", ""),
            industry=command.get("industry", ""),
        )
        if any(spec.values()):
            obs.process.emit(
                "profile_selection_applied",
                **{k: v for k, v in spec.items() if v},
                matched=len(profiles),
                of=len(build.profiles),
            )
            if not profiles:
                yield self._say(
                    ctx,
                    f"Selection matched 0 of the {len(build.profiles)} built profiles "
                    f"(filter: { {k: v for k, v in spec.items() if v} }).\n"
                    "Nothing was pushed. Check the ids/industry and try again — "
                    "I never widen a selection to everything.",
                )
                return
            yield self._say(
                ctx,
                f"Selection: {len(profiles)} of {len(build.profiles)} profiles match "
                f"{ {k: v for k, v in spec.items() if v} }",
            )
        limit = command.get("limit", self.default_limit)
        if limit:
            profiles = profiles[:limit]
            obs.process.emit("profile_limit_applied", limit=limit, pushing=len(profiles))

        # STEP 4 (+A+5) — systemic failure halts; taxonomy split is preserved.
        try:
            summary = await asyncio.to_thread(push_profiles, profiles)
        except SystemicFailure as exc:
            yield self._say(
                ctx,
                f"HALTED — systemic failure (auth/config): {exc}\n"
                "This affects every lead; no individual lead is at fault. "
                "Nothing was recorded per-record. Fix the configuration and rerun.",
                state={"halted": True},
            )
            return

        lines = [
            "Run complete." if not summary.halted else f"RUN HALTED: {summary.halt_reason}",
            f"  profiles built:    {len(build.profiles)}"
            + (f" (pushed first {len(profiles)})" if limit else ""),
            f"  pushed:            {summary.pushed}",
            f"  failed_record:     {summary.failed_record}"
            + ("  -> errors/schema_mismatch.jsonl" if summary.failed_record else ""),
            f"  failed_transport:  {summary.failed_transport}"
            + ("  -> errors/transport_failures.jsonl" if summary.failed_transport else ""),
            f"  not_attempted:     {summary.summary['not_attempted']}",
            f"  unresolved:        {len(build.unresolved)}"
            + ("  -> errors/unresolved.jsonl" if build.unresolved else ""),
            f"  run_id:            {summary.run_id}",
            "No lead was dropped: every non-pushed lead is on file with a reason.",
        ]
        yield self._say(
            ctx,
            "\n".join(lines),
            state={
                "pushed": summary.pushed,
                "failed": summary.failed,
                "halted": summary.halted,
            },
        )

    # -- counting (no HubSpot calls, no files written) ---------------------------

    async def _do_count(self, ctx: InvocationContext, command: dict[str, Any]):
        base = Path(
            command.get("incoming_dir") or self.incoming_dir or default_incoming_dir()
        )
        try:
            feed = await asyncio.to_thread(load_csvs, base)
        except CsvStructureError as exc:
            yield self._say(ctx, f"Cannot count — structural feed failure: {exc}")
            return
        build = await asyncio.to_thread(build_profiles, feed)
        matched, spec = filter_profiles(
            build.profiles,
            employee_ids=command.get("employee_ids", ""),
            company_ids=command.get("company_ids", ""),
            industry=command.get("industry", ""),
        )
        active = {k: v for k, v in spec.items() if v}
        yield self._say(
            ctx,
            (
                f"{len(matched)} of {len(build.profiles)} built profiles match {active}."
                if active
                else f"{len(build.profiles)} decision-maker profiles built "
                f"({len(build.unresolved)} unresolved)."
            )
            + " Nothing was pushed.",
        )

    # -- the read path ----------------------------------------------------------

    async def _do_lookup(self, ctx: InvocationContext, command: dict[str, Any]):
        record = await asyncio.to_thread(
            get_lead_profile,
            command.get("employee_id"),
            command.get("email"),
        )
        payload = record.to_dict()
        if not payload["found"]:
            yield self._say(ctx, "Not found in HubSpot.")
            return
        note = ""
        if not payload["company_resolved"]:
            note = f"\nWARNING: company unresolved — {'; '.join(payload['warnings'])}"
        yield self._say(
            ctx,
            f"Found. contact_hs_id={payload['contact_hs_id']} "
            f"company_hs_id={payload['company_hs_id']}\n"
            f"profile: {payload['profile']}{note}",
        )

    # -- plumbing ----------------------------------------------------------------

    def _say(
        self, ctx: InvocationContext, text: str, state: dict[str, Any] | None = None
    ) -> Event:
        """Yield text; session-state changes ride the event as a state_delta.

        In a custom BaseAgent, mutating ``ctx.session.state`` directly does not
        persist — the session service applies deltas carried by events. This is
        the documented mechanism, and it keeps state changes auditable: every
        change is on exactly one event.
        """
        return Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta=state or {}),
        )
