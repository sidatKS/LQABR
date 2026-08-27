"""The deterministic run: fetch -> summarise -> write. One function.

This is what POST /summary/run and the CLI call. There is no session, no
tool-choice loop and no second orchestrator — the model is used for the one
thing that needs judgement (the summary) and everything around it is
ordinary code, so the same request always takes the same path.

The ADK agent (agent.py) is the conversational face of the SAME steps, over
the same tools and the same prompt. Two surfaces, one behaviour.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import requests

from summary_core import sources
from summary_core.mcp.hubspot import HubSpotMCP
from summary_core.obs import Observability, get_obs, new_run_id
from summary_core.settings import Settings, get_settings
from summary_core.types import SourceError, WriteResult

from schema import (
    HubSpotOutcome,
    SourceInfo,
    SummaryRequest,
    SummaryResponse,
    SummaryValidationError,
    to_payload,
)
from summarizer import summarize


def run_summary(request: SummaryRequest, *,
                settings: Settings | None = None,
                obs: Observability | None = None,
                hubspot: HubSpotMCP | None = None,
                session: Optional[requests.Session] = None,
                completion: Optional[Callable[..., Any]] = None) -> SummaryResponse:
    """One run. Never raises for a bad source or a bad model answer — those
    come back as `status="failed"` with the reason, because a caller needs to
    know WHICH step failed, not just that something did."""
    settings = settings or get_settings()
    obs = obs or get_obs(new_run_id(), refresh=True)
    source_info = SourceInfo()

    # ---- step 1: normalise the input, whatever kind it was ----------
    spec = request.to_spec()
    source_info.kind = spec.kind
    source_info.reference = spec.reference
    obs.process.emit("run_start", source_kind=spec.kind, source_ref=spec.reference,
                     object_id=(request.hubspot.object_id if request.hubspot else ""),
                     model=settings.model, dry_run=settings.dry_run)

    with obs.step("fetch", source_kind=spec.kind, source_ref=spec.reference) as step:
        try:
            document = sources.fetch(spec, settings, session=session, obs=obs)
        except SourceError as exc:
            step.failed(str(exc))
            obs.process.emit("run_failed", step="fetch", reason=str(exc))
            return SummaryResponse(run_id=obs.run_id, status="failed",
                                   source=source_info, model=settings.model,
                                   error=str(exc))
        step.ok(title=document.title, chars=document.char_count,
                truncated=document.truncated)

    source_info.title = document.title
    source_info.chars = document.char_count
    source_info.truncated = document.truncated

    # ---- step 2: summarise ------------------------------------------
    with obs.step("summarize", chars=document.char_count, model=settings.model,
                  source_kind=spec.kind) as step:
        try:
            result = summarize(document, settings=settings, obs=obs,
                               completion=completion)
        except SummaryValidationError as exc:
            step.failed(str(exc))
            obs.process.emit("run_failed", step="summarize", reason=str(exc))
            return SummaryResponse(run_id=obs.run_id, status="failed",
                                   source=source_info, model=settings.model,
                                   error=str(exc))
        except Exception as exc:
            # The provider itself failed — no key, rate limit, outage. That is
            # a reported outcome with the step named, not a 500 with a provider
            # stack trace: the caller asked a valid question, and an operator
            # needs to know it was the MODEL hop that broke.
            reason = f"the model call failed ({type(exc).__name__}): {exc}"
            step.failed(reason)
            obs.process.emit("run_failed", step="summarize", reason=reason)
            return SummaryResponse(run_id=obs.run_id, status="failed",
                                   source=source_info, model=settings.model,
                                   error=reason)
        step.ok(chars=len(result.summary), key_points=len(result.key_points),
                industry=result.industry)

    # ---- step 3: land it on HubSpot ---------------------------------
    target = request.hubspot
    blog_style = settings.mcp_write_style == "blog_summary"
    if target is None:
        outcome = WriteResult(status="skipped", object_type=settings.hubspot_object_type,
                              error="no hubspot target was supplied, so nothing was written")
    elif blog_style and not str(target.blog_published_at or "").strip():
        # blog_summary style is keyed on blog_published_at; without it there is
        # no row to upsert. A summarise-only call is still legitimate.
        outcome = WriteResult(status="skipped", object_type=settings.hubspot_object_type,
                              error="no hubspot.blog_published_at was supplied, so nothing was written")
    elif not blog_style and not str(target.object_id or "").strip():
        outcome = WriteResult(status="skipped", object_type=settings.hubspot_object_type,
                              error="no hubspot.object_id was supplied, so nothing was written")
    else:
        hubspot = hubspot or HubSpotMCP(settings=settings, obs=obs)
        with obs.step("write_summary", object_id=target.object_id,
                      object_type=target.object_type or settings.hubspot_object_type,
                      style=settings.mcp_write_style,
                      dry_run=settings.dry_run) as step:
            outcome = hubspot.write_summary(
                target.object_id, result,
                industry=target.industry,
                object_type=target.object_type,
                subject=target.subject,
                blog_published_at=target.blog_published_at,
                extra_properties=target.properties,
            )
            written = {"write_status": outcome.status}
            if outcome.status == "skipped":
                step.skipped(outcome.error or outcome.status, **written)
            elif outcome.ok:
                step.ok(**written)
            else:
                step.failed(outcome.error, **written)

    response = SummaryResponse(
        run_id=obs.run_id,
        # The summary succeeded; a failed WRITE is reported in `hubspot`, and
        # `status` follows it so a caller polling one field is not misled.
        status="completed" if outcome.ok else "failed",
        source=source_info,
        summary=to_payload(result),
        hubspot=HubSpotOutcome(**outcome.to_dict()),
        model=settings.model,
        error="" if outcome.ok else outcome.error,
    )
    obs.process.emit("run_complete", status=response.status, write_status=outcome.status,
                     source_kind=source_info.kind, chars=source_info.chars)
    return response
