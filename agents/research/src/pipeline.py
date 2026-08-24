"""One run, start to finish.

    read the lead        (MCP)
    read the blog post   (MCP)
    research + compose   (web search + model)
    write lead_context   (MCP)  -> raises HubSpot trigger 2 -> Email agent

Never raises for a bad lead, a missing post or a failed model call — those come
back as ``status="failed"`` with the reason and the step named, because a caller
needs to know WHICH step failed, not just that something did.
"""

from __future__ import annotations

from typing import Optional

try:
    from research_core.mcp.hubspot import HubSpotMCP
    from research_core.obs import Observability, get_obs, new_run_id
    from research_core.settings import Settings, get_settings
    from research_core.types import ResearchNote
except ImportError:  # pragma: no cover
    from ..packages.research_core.mcp.hubspot import HubSpotMCP  # type: ignore
    from ..packages.research_core.obs import (  # type: ignore
        Observability, get_obs, new_run_id)
    from ..packages.research_core.settings import Settings, get_settings  # type: ignore
    from ..packages.research_core.types import ResearchNote  # type: ignore

from composer import Composer, SearchError
from schema import HubSpotOutcome, ResearchResponse, ResearchTarget


def run_research(target: ResearchTarget, *,
                 settings: Settings | None = None,
                 obs: Observability | None = None,
                 hubspot: Optional[HubSpotMCP] = None,
                 composer: Optional[Composer] = None,
                 run_id: str = "") -> ResearchResponse:
    settings = settings or get_settings()
    obs = obs or get_obs(run_id or new_run_id(), refresh=True)
    hubspot = hubspot or HubSpotMCP(settings=settings, obs=obs)

    obs.process.emit("run_start", object_id=target.object_id,
                     blog_published_at=target.blog_published_at,
                     summary_ref_id=target.summary_ref_id, dry_run=settings.dry_run)

    def _failed(step: str, reason: str) -> ResearchResponse:
        obs.process.emit("run_failed", step=step, object_id=target.object_id, reason=reason)
        return ResearchResponse(run_id=obs.run_id, status="failed",
                                object_id=target.object_id, model=settings.model,
                                error=reason)

    # ---- step 1: the lead -------------------------------------------
    if not target.object_id:
        return _failed("input", "bad-data: no object_id was supplied, so there is "
                                "no lead to research")
    lead = hubspot.read_lead(target.object_id)
    if lead is None:
        return _failed("read_lead", f"crm-error: the MCP returned no lead for "
                                    f"object_id {target.object_id}")

    # TEST AFFORDANCE (gap B1): a caller-supplied company name wins over the
    # MCP, which currently returns company_id but no name. Logged loudly so a
    # run using it is never mistaken for one where the MCP supplied the name.
    if target.company:
        obs.process.emit("company_override_applied", object_id=target.object_id,
                         supplied=target.company, from_mcp=lead.company,
                         reason="TEST ONLY — gap B1; the gateway never sends this")
        lead.company = target.company

    if settings.skip_if_context_present and lead.existing_lead_context.strip():
        obs.process.emit("run_skipped", object_id=target.object_id,
                         reason="lead_context already present and "
                                "LQABR_RESEARCH_SKIP_IF_CONTEXT_PRESENT=1")
        return ResearchResponse(
            run_id=obs.run_id, status="completed", object_id=target.object_id,
            lead=lead.to_dict(), model=settings.model,
            hubspot=HubSpotOutcome(status="skipped", object_id=target.object_id,
                                   property_name=settings.hubspot_context_property,
                                   tool=settings.mcp_tool_write,
                                   error="lead_context already present"))

    # ---- step 2: the published post ---------------------------------
    if not target.blog_published_at:
        return _failed("input", "bad-data: no blog_published_at was supplied — the MCP "
                                "reads the blog store by publication timestamp, so the "
                                "post cannot be fetched without it")
    blog = hubspot.read_blog(target.blog_published_at)
    if blog is None or not blog.usable:
        return _failed("read_blog", f"crm-error: no blog summary found for "
                                    f"blog_published_at {target.blog_published_at}")

    # ---- step 3: research + compose ---------------------------------
    composer = composer or Composer(settings=settings, obs=obs)
    try:
        note: ResearchNote = composer.compose(lead, blog)
    except SearchError as exc:
        return _failed("research", str(exc))
    except Exception as exc:  # noqa: BLE001 - the step is named, not swallowed
        return _failed("research", f"the research pass failed "
                                   f"({type(exc).__name__}): {exc}")

    # ---- step 4: write it back --------------------------------------
    outcome = hubspot.write_context(lead, note)

    response = ResearchResponse(
        run_id=obs.run_id,
        # status follows the WRITE: a note that could not be landed is a failure,
        # and the note still comes back so the work is not lost.
        status="completed" if outcome.ok else "failed",
        object_id=target.object_id,
        lead=lead.to_dict(),
        blog=blog.to_dict(),
        note=note.text,
        sources=note.sources,
        hubspot=HubSpotOutcome(**outcome.to_dict()),
        model=settings.model,
        error="" if outcome.ok else outcome.error,
    )
    obs.process.emit("run_complete", status=response.status,
                     write_status=outcome.status, object_id=target.object_id,
                     chars=outcome.chars, sources=len(note.sources))
    return response
