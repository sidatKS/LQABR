"""The lead_profile agent's ADK tool surface — Steps 1-6 as callable tools.

The agent is now invoked, not scripted: ``adk run agents/lead_profile/src``
or ``adk web agents`` drives the whole CSV -> HubSpot flow as ONE agentic run,
and the model decides the sequence.

Two rules make that safe, and they are the reason this file exists rather than
the model calling the step functions directly.

1. THE DATA NEVER ENTERS THE MODEL'S CONTEXT.
   ``build_lead_profiles`` returns counts, not 263 profiles. The profiles live
   in a process-local store keyed by the ADK invocation id; session state holds
   only small JSON-serialisable markers. The model orchestrates the pipeline;
   it never sees, edits or hallucinates a lead.

2. THE MODEL CANNOT REORDER OR SKIP A STEP.
   Every tool asserts its precondition and returns a typed, self-explaining
   error telling the model what to run first. Calling ``push_lead_profiles``
   before ``build_lead_profiles`` is refused, not half-executed.

Everything blocking runs in a worker thread (``asyncio.to_thread``) so a 263-
lead push does not stall ADK's event loop and freeze ``adk web``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext

from lqabr_core.obs import get_obs
from lqabr_core.leadgen.hubspot.crm import (
    HubSpotHttp,
    get_lead_profile as _get_lead_profile,
    record_unresolved,
)
from lqabr_core.leadgen.hubspot.failures import SystemicFailure

# Co-located modules; support both package (ADK) and flat (tests) import styles.
try:
    from .build_profile import BuildResult, build_profiles
    from .call_mcp import push_profiles
    from .load_csv import CsvStructureError, default_incoming_dir, load_csvs
except ImportError:  # pragma: no cover
    from build_profile import BuildResult, build_profiles  # type: ignore
    from call_mcp import push_profiles  # type: ignore
    from load_csv import CsvStructureError, default_incoming_dir, load_csvs  # type: ignore

# ---------------------------------------------------------------------------
# Process-local run store.
#
# ADK session state must stay small and JSON-serialisable (it is persisted by
# the session service and replayed into the model's context). RawFeed and
# list[LeadProfile] are neither. So the heavy objects live here, keyed by the
# ADK invocation id, and session state holds only counts and flags.
# ---------------------------------------------------------------------------

_RUN_STORE: dict[str, dict[str, Any]] = {}


def _slot(tool_context: ToolContext | None) -> dict[str, Any]:
    key = getattr(tool_context, "invocation_id", None) or "default"
    return _RUN_STORE.setdefault(key, {})


def clear_run_store(invocation_id: str | None = None) -> None:
    """Drop cached feed/profiles. Called when an invocation ends."""
    if invocation_id is None:
        _RUN_STORE.clear()
    else:
        _RUN_STORE.pop(invocation_id, None)


def _state(tool_context: ToolContext | None, key: str, value: Any) -> None:
    if tool_context is not None:
        try:
            tool_context.state[key] = value
        except Exception:  # pragma: no cover - state is best-effort telemetry
            pass


def _error(step: str, message: str, next_step: str) -> dict[str, Any]:
    """A refusal the model can act on, rather than a stack trace it will guess at."""
    return {
        "ok": False,
        "step": step,
        "error": message,
        "next_step": next_step,
    }


# ===========================================================================
# STEP 1 + 2 — the CSV feed
# ===========================================================================


async def load_csv_feed(incoming_dir: str, tool_context: ToolContext) -> dict[str, Any]:
    """Step 1+2: read the three seed CSVs from the incoming directory into memory.

    Reads employees_with_company_sample.csv, employee_contacts_5234.csv and
    companies_clean_734.csv, asserting the required columns of each. This is the
    first step of the run — nothing else can be called before it.

    Args:
        incoming_dir: Directory holding the three CSVs. Pass an empty string to
            use the configured default (LQABR_INCOMING_DIR, else data/incoming).

    Returns:
        Row counts per file, or a structural error naming the exact file and
        column that is missing. A structural error HALTS the run: it is a
        broken feed, not a bad record, and no lead should be pushed.
    """
    obs = get_obs()
    base = Path(incoming_dir) if incoming_dir.strip() else default_incoming_dir()

    present = [
        name
        for name in (
            "employees_with_company_sample.csv",
            "employee_contacts_5234.csv",
            "companies_clean_734.csv",
        )
        if (base / name).exists()
    ]
    obs.process.emit(
        "step1_trigger_detected",
        step=1,
        name="csv_feed_drop",
        incoming_dir=str(base),
        files_present=present,
        trigger="agent",
    )

    try:
        feed = await asyncio.to_thread(load_csvs, base)
    except CsvStructureError as exc:
        _state(tool_context, "feed_loaded", False)
        return {
            "ok": False,
            "step": "load_csv_feed",
            "error": f"structural feed failure: {exc}",
            "halt": True,
            "next_step": (
                "Stop. Do not build or push anything. Report the missing file or "
                "column to the operator — the feed itself is broken."
            ),
        }

    _slot(tool_context)["feed"] = feed
    _state(tool_context, "feed_loaded", True)
    _state(tool_context, "feed_counts", feed.counts)
    return {
        "ok": True,
        "step": "load_csv_feed",
        "incoming_dir": str(base),
        "rows": feed.counts,
        "next_step": "build_lead_profiles",
    }


# ===========================================================================
# STEP 3 — build the in-memory profile list
# ===========================================================================


async def build_lead_profiles(tool_context: ToolContext) -> dict[str, Any]:
    """Step 3: join the three feeds into the decision-maker-only lead list, in memory.

    Keeps rows where Decision_Maker_Flag is "yes", resolves each against the
    contacts and companies files, and produces one LeadProfile per lead. Rows
    whose contact or company does not resolve are flagged as unresolved and set
    aside — never pushed, never discarded.

    The profiles themselves are held in memory and are deliberately NOT returned:
    only counts come back. Never ask for the profile list.

    Returns:
        Counts: rows seen, decision makers seen, profiles built, unresolved, and
        any duplicate source rows that were reported rather than silently dropped.
    """
    slot = _slot(tool_context)
    feed = slot.get("feed")
    if feed is None:
        return _error(
            "build_lead_profiles",
            "no CSV feed in memory for this run",
            "call load_csv_feed first",
        )

    build: BuildResult = await asyncio.to_thread(build_profiles, feed)
    slot["build"] = build

    # B14: unresolved leads are persisted, not just counted in a log line.
    await asyncio.to_thread(
        record_unresolved, [lead.to_dict() for lead in build.unresolved]
    )

    _state(tool_context, "profiles_built", len(build.profiles))
    _state(tool_context, "unresolved", len(build.unresolved))
    return {
        "ok": True,
        "step": "build_lead_profiles",
        **build.summary,
        "reconciles": build.decision_makers_seen
        == len(build.profiles) + len(build.unresolved) + build.duplicate_employee_rows,
        "note": "profiles are held in memory; unresolved leads were written to errors/unresolved.jsonl",
        "next_step": "push_lead_profiles",
    }


# ===========================================================================
# Selection — deterministic filters over the in-memory profile list.
#
# This is what lets an operator (or the LLM console mapping the operator's
# English) push a SUBSET: "only company C123", "only manufacturing", "just
# E1042 and E1077". The model supplies structured filter values; this code —
# not the model — decides which leads match. Filters are logged, so which
# subset ran is always on the record.
# ===========================================================================


def _csv_set(raw: str) -> set[str] | None:
    values = {part.strip() for part in (raw or "").split(",") if part.strip()}
    return values or None


def filter_profiles(
    profiles: list,
    *,
    employee_ids: str = "",
    company_ids: str = "",
    industry: str = "",
) -> tuple[list, dict[str, Any]]:
    """Apply the selection filters. Deterministic; returns (matched, spec)."""
    want_employees = _csv_set(employee_ids)
    want_companies = _csv_set(company_ids)
    want_industry = (industry or "").strip().upper() or None

    matched = [
        profile
        for profile in profiles
        if (want_employees is None or profile.employee_id in want_employees)
        and (want_companies is None or profile.company_id in want_companies)
        and (want_industry is None or (profile.industry or "") == want_industry)
    ]
    spec = {
        "employee_ids": sorted(want_employees) if want_employees else None,
        "company_ids": sorted(want_companies) if want_companies else None,
        "industry": want_industry,
    }
    return matched, spec


async def count_lead_profiles(
    tool_context: ToolContext,
    employee_ids: str = "",
    company_ids: str = "",
    industry: str = "",
) -> dict[str, Any]:
    """Count how many BUILT profiles match a selection — counts only, no lead data.

    Use this before a selective push to confirm the selection matches what the
    operator expects ("push the manufacturing leads" -> check how many first).

    Args:
        employee_ids: Comma-separated Employee_IDs to match (e.g. "E1042,E1077").
            Pass an empty string for no employee filter.
        company_ids: Comma-separated Company_IDs to match. Empty string = no filter.
        industry: One industry to match, case-insensitive (e.g. "manufacturing").
            Empty string = no filter.

    Returns:
        total built, matched by this selection, and the normalised filter spec.
        Never returns the lead records themselves.
    """
    build: BuildResult | None = _slot(tool_context).get("build")
    if build is None:
        return _error(
            "count_lead_profiles",
            "no profiles have been built for this run",
            "call load_csv_feed, then build_lead_profiles, then retry",
        )
    matched, spec = filter_profiles(
        build.profiles,
        employee_ids=employee_ids,
        company_ids=company_ids,
        industry=industry,
    )
    return {
        "ok": True,
        "step": "count_lead_profiles",
        "total_built": len(build.profiles),
        "matched": len(matched),
        "filter": spec,
    }


# ===========================================================================
# STEP 4 (+ A + 5) — push through the in-process MCP
# ===========================================================================


async def push_lead_profiles(
    tool_context: ToolContext,
    limit: int = 0,
    employee_ids: str = "",
    company_ids: str = "",
    industry: str = "",
) -> dict[str, Any]:
    """Step 4+A+5: push built leads into HubSpot — all of them, or a selection.

    For each pushed profile: assign a unique lead_ref_id, mint a fresh M2M
    token, then upsert the Company, upsert the Contact and associate them.
    Creating and updating are one upsert — running this twice is safe and does
    not duplicate records.

    Args:
        limit: Push at most the first N of the selection. Pass 0 for no cap.
        employee_ids: Comma-separated Employee_IDs to push (e.g. "E1042,E1077").
            Empty string = do not filter by employee.
        company_ids: Comma-separated Company_IDs to push. Empty string = no filter.
        industry: Push only leads whose company industry matches (case-
            insensitive, e.g. "manufacturing"). Empty string = no filter.

    Returns:
        The run summary: matched (by the selection), pushed, failed_record (bad
        data), failed_transport (dependency trouble), and whether the run halted
        early. A halted run means HubSpot or the credentials are the problem,
        NOT the leads — report that and do not retry in a loop.
    """
    slot = _slot(tool_context)
    build: BuildResult | None = slot.get("build")
    if build is None:
        return _error(
            "push_lead_profiles",
            "no profiles have been built for this run",
            "call load_csv_feed, then build_lead_profiles, then retry",
        )

    profiles, spec = filter_profiles(
        build.profiles,
        employee_ids=employee_ids,
        company_ids=company_ids,
        industry=industry,
    )
    if any(spec.values()):
        get_obs().process.emit(
            "profile_selection_applied", **{k: v for k, v in spec.items() if v},
            matched=len(profiles), of=len(build.profiles),
        )
        if not profiles:
            return {
                "ok": False,
                "step": "push_lead_profiles",
                "error": "the selection matched 0 of the built profiles",
                "filter": spec,
                "total_built": len(build.profiles),
                "next_step": (
                    "check the filter values with count_lead_profiles, or ask the "
                    "operator to confirm the ids/industry — do NOT push everything instead"
                ),
            }
    if limit and limit > 0:
        profiles = profiles[:limit]
        get_obs().process.emit("profile_limit_applied", limit=limit, pushing=len(profiles))

    if not profiles:
        return {
            "ok": True,
            "step": "push_lead_profiles",
            "pushed": 0,
            "note": "no qualified profiles to push",
        }

    http: HubSpotHttp | None = slot.get("http")
    try:
        summary = await asyncio.to_thread(push_profiles, profiles, http=http)
    except SystemicFailure as exc:
        return {
            "ok": False,
            "step": "push_lead_profiles",
            "error": f"systemic failure: {exc}",
            "halt": True,
            "next_step": (
                "Stop. This is an auth or configuration problem that affects every "
                "lead — no lead is at fault. Report it and do not retry."
            ),
        }

    slot["summary"] = summary
    _state(tool_context, "pushed", summary.pushed)
    _state(tool_context, "failed", summary.failed)
    _state(tool_context, "halted", summary.halted)

    payload: dict[str, Any] = {
        "ok": not summary.halted,
        "step": "push_lead_profiles",
        **summary.summary,
        "matched_by_selection": len(profiles),
        "filter": spec if any(spec.values()) else None,
        "profiles_built_total": len(build.profiles),
        "unresolved": len(build.unresolved),
    }
    if summary.halted:
        # The dependency is down or misconfigured. Leads that were never
        # attempted are reported as not_attempted — NOT as failures.
        payload["halt"] = True
        payload["error"] = summary.halt_reason
        payload["next_step"] = (
            "Stop. This is a dependency or configuration problem affecting every "
            "remaining lead — no individual lead is at fault. Report it verbatim "
            "and do not retry."
        )
    else:
        payload["next_step"] = "report the summary to the operator"
    return payload


# ===========================================================================
# Unresolved leads — the never-drop-a-lead evidence
# ===========================================================================


async def list_unresolved_leads(tool_context: ToolContext) -> dict[str, Any]:
    """List the decision-maker rows that could not be joined to a contact or company.

    These are flagged, kept and never pushed. Use this to explain to the
    operator exactly which leads did not make it and why.

    Returns:
        Up to 50 unresolved leads with their employee_id, company_id and reason,
        plus the total count.
    """
    build: BuildResult | None = _slot(tool_context).get("build")
    if build is None:
        return _error(
            "list_unresolved_leads",
            "no profiles have been built for this run",
            "call load_csv_feed, then build_lead_profiles",
        )
    entries = [lead.to_dict() for lead in build.unresolved]
    return {
        "ok": True,
        "step": "list_unresolved_leads",
        "total": len(entries),
        "shown": min(50, len(entries)),
        "unresolved": entries[:50],
    }


# ===========================================================================
# STEP 6 — the shared read path
# ===========================================================================


async def get_lead_profile(employee_id: str, email: str) -> dict[str, Any]:
    """Step 6: read one lead's current state from HubSpot. Read-only, never writes.

    This is the shared read path the email, voice and scheduler agents also use.

    Args:
        employee_id: The primary lookup key. Pass an empty string to look up by email.
        email: Fallback lookup key. Searches HubSpot's STANDARD "email"
            property (decided 2026-08-26; the custom "email_id" is retired).
            Pass an empty string when using employee_id.

    Returns:
        The nine-field lead profile plus contact_hs_id and company_hs_id, or
        found=false. Check company_resolved: when it is false the company fields
        are unpopulated and the warnings list says why.
    """
    employee_id = (employee_id or "").strip()
    email = (email or "").strip()
    if not employee_id and not email:
        return _error(
            "get_lead_profile",
            "a lookup key is required",
            "call again with employee_id (preferred) or email",
        )
    record = await asyncio.to_thread(
        _get_lead_profile, employee_id or None, email or None
    )
    return {"ok": True, "step": "get_lead_profile", **record.to_dict()}


PIPELINE_TOOLS = [
    load_csv_feed,
    build_lead_profiles,
    count_lead_profiles,
    push_lead_profiles,
    list_unresolved_leads,
    get_lead_profile,
]
