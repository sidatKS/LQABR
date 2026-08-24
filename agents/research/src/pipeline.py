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

from typing import List, Optional

try:
    from research_core.mcp.hubspot import HubSpotMCP
    from research_core.obs import Observability, get_obs, new_run_id
    from research_core.settings import Settings, get_settings
    from research_core.types import BlogFacts, ResearchNote
except ImportError:  # pragma: no cover
    from ..packages.research_core.mcp.hubspot import HubSpotMCP  # type: ignore
    from ..packages.research_core.obs import (  # type: ignore
        Observability, get_obs, new_run_id)
    from ..packages.research_core.settings import Settings, get_settings  # type: ignore
    from ..packages.research_core.types import BlogFacts, ResearchNote  # type: ignore

from composer import Composer, SearchError
from schema import (CampaignLeadResult, CampaignResponse, CampaignTarget,
                    HubSpotOutcome, ResearchResponse, ResearchTarget)


def run_research(target: ResearchTarget, *,
                 settings: Settings | None = None,
                 obs: Observability | None = None,
                 hubspot: Optional[HubSpotMCP] = None,
                 composer: Optional[Composer] = None,
                 blog: Optional[BlogFacts] = None,
                 run_id: str = "") -> ResearchResponse:
    settings = settings or get_settings()
    obs = obs or get_obs(run_id or new_run_id(), refresh=True)
    hubspot = hubspot or HubSpotMCP(settings=settings, obs=obs)

    obs.process.emit("run_start", object_id=target.object_id,
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
    # `blog` is passed in by the campaign path, which has already read it once
    # — without that, a campaign over N leads would fetch the same post N times.
    if blog is None:
        # summary_ref_id is the BLOG POST's record id. target.object_id is the
        # contact's — reading the blog by that would fetch the wrong record.
        if not target.summary_ref_id:
            return _failed("input", "bad-data: no summary_ref_id was supplied — the "
                                    "MCP reads the blog store by the post's record "
                                    "id, so the post cannot be fetched without it")
        blog = hubspot.read_blog(target.summary_ref_id)
        if blog is None or not blog.usable:
            return _failed("read_blog", f"crm-error: no blog summary found for "
                                        f"object_id {target.summary_ref_id}")

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


def run_campaign(target: CampaignTarget, *,
                 settings: Settings | None = None,
                 obs: Observability | None = None,
                 hubspot: Optional[HubSpotMCP] = None,
                 composer: Optional[Composer] = None,
                 run_id: str = "") -> CampaignResponse:
    """One published post -> every lead in its industry.

        read the post        (MCP, by its object_id)
        take its industry    (blog_industry, unless the caller overrode it)
        list the leads       <- how many leads that industry has
        research + write     per lead, independently

    Each lead is worked on its own: one lead's failure is recorded against
    that lead and the campaign carries on, because a single bad record must
    never cost the other leads their context. The reply carries the counts
    (`leads_found`, `written`, `failed`, `skipped`) and a per-lead breakdown.
    """
    settings = settings or get_settings()
    obs = obs or get_obs(run_id or new_run_id(), refresh=True)
    hubspot = hubspot or HubSpotMCP(settings=settings, obs=obs)

    obs.process.emit("campaign_start", object_id=target.object_id,
                     industry_override=target.industry, limit=target.limit,
                     dry_run=settings.dry_run)

    def _failed(step: str, reason: str, **extra) -> CampaignResponse:
        obs.process.emit("campaign_failed", step=step,
                         object_id=target.object_id, reason=reason)
        return CampaignResponse(run_id=obs.run_id, status="failed",
                                object_id=target.object_id,
                                model=settings.model, error=reason, **extra)

    # ---- step 1: the post --------------------------------------------
    # The MCP reads the blog store by the Ticket's own record id, which is
    # exactly what the gateway's blog-summary route hands over.
    if not target.object_id:
        return _failed("input",
                       "bad-data: no object_id was supplied, so there is no "
                       "blog post to build a campaign from")
    blog = hubspot.read_blog(target.object_id)
    if blog is None or not blog.usable:
        return _failed("read_blog", f"crm-error: no blog summary found for "
                                    f"object_id {target.object_id}")
    obs.process.emit("campaign_blog_read", object_id=target.object_id,
                     blog_industry=blog.blog_industry,
                     summary_chars=len(blog.blog_summary))

    industry = (target.industry or blog.blog_industry or "").strip()
    if not industry:
        return _failed("industry",
                       "bad-data: the post carries no blog_industry and none was "
                       "supplied, so there is no industry to select leads by",
                       blog=blog.to_dict())
    if target.industry and target.industry != blog.blog_industry:
        obs.process.emit("campaign_industry_override", object_id=target.object_id,
                         supplied=target.industry, from_post=blog.blog_industry)

    # ---- step 2: who is in that industry -----------------------------
    lead_ids = hubspot.list_leads_by_industry(industry, limit=target.limit)
    if lead_ids is None:
        # "could not ask" is NOT "nobody matched" — refuse rather than report
        # an empty campaign that silently skipped every lead.
        return _failed("list_leads",
                       f"crm-error: could not list leads for industry {industry!r} "
                       "— the campaign is not run rather than run against an "
                       "unknown set of leads",
                       industry=industry, blog=blog.to_dict())

    obs.process.emit("campaign_leads_found", object_id=target.object_id,
                     industry=industry, leads_found=len(lead_ids),
                     leads=lead_ids[:20])
    if not lead_ids:
        # A real, valid answer: nobody matched. Not a failure.
        obs.process.emit("campaign_complete", object_id=target.object_id,
                         status="completed", industry=industry, leads_found=0,
                         written=0, failed=0, skipped=0,
                         note="no lead is in this industry")
        return CampaignResponse(
            run_id=obs.run_id, status="completed", object_id=target.object_id,
            industry=industry, leads_found=0, blog=blog.to_dict(),
            model=settings.model)

    # ---- step 3: one lead at a time ----------------------------------
    # The blog is passed down already-read: a campaign over N leads must not
    # fetch the same post N times.
    composer = composer or Composer(settings=settings, obs=obs)
    results: List[CampaignLeadResult] = []
    for position, lead_object_id in enumerate(lead_ids, start=1):
        obs.process.emit("campaign_lead_start", object_id=lead_object_id,
                         position=position, of=len(lead_ids), industry=industry)
        one = run_research(
            ResearchTarget(object_id=lead_object_id,
                           summary_ref_id=target.object_id),
            settings=settings, obs=obs, hubspot=hubspot,
            composer=composer, blog=blog, run_id=obs.run_id)
        write_status = (one.hubspot.status if one.hubspot else "") or one.status
        result = CampaignLeadResult(
            object_id=lead_object_id,
            status="skipped" if write_status == "skipped" else one.status,
            chars=one.hubspot.chars if one.hubspot else 0,
            error=one.error)
        results.append(result)
        obs.process.emit("campaign_lead_done", object_id=lead_object_id,
                         position=position, of=len(lead_ids),
                         status=result.status, chars=result.chars,
                         error=result.error[:200])

    written = sum(1 for r in results if r.status == "completed")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    status = ("completed" if failed == 0
              else "failed" if written == 0 and skipped == 0
              else "partial")

    obs.process.emit("campaign_complete", object_id=target.object_id,
                     status=status, industry=industry,
                     leads_found=len(lead_ids), written=written,
                     failed=failed, skipped=skipped,
                     failed_leads=[r.object_id for r in results
                                   if r.status == "failed"][:20])
    return CampaignResponse(
        run_id=obs.run_id, status=status, object_id=target.object_id,
        industry=industry, leads_found=len(lead_ids), written=written,
        failed=failed, skipped=skipped, results=results, blog=blog.to_dict(),
        model=settings.model,
        error="" if status == "completed" else f"{failed} of {len(lead_ids)} leads failed")
