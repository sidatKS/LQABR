"""One run, start to finish.

    read the lead        (MCP)
    read the blog post   (MCP)
    research + compose   (web search + model)
    write lead_context   (MCP)  -> raises HubSpot trigger 2 -> Email agent

Never raises for a bad lead, a missing post or a failed model call — those come
back as ``status="failed"`` with the reason and the step named, because a caller
needs to know WHICH step failed, not just that something did.

Every stage runs inside ``obs.step``, so the log carries its inputs, its outputs
and its duration whichever way the block is left. Read top to bottom, the log is
the run.
"""

from __future__ import annotations

from typing import List, Optional

try:
    from research_core.mcp.hubspot import HubSpotMCP
    from research_core.research_logging import ResearchLogging, get_obs, new_run_id, preview
    from research_core.settings import Settings, get_settings
    from research_core.types import BlogFacts, ResearchNote
except ImportError:  # pragma: no cover
    from ..packages.research_core.mcp.hubspot import HubSpotMCP  # type: ignore
    from ..packages.research_core.research_logging import (  # type: ignore
        ResearchLogging, get_obs, new_run_id, preview)
    from ..packages.research_core.settings import Settings, get_settings  # type: ignore
    from ..packages.research_core.types import BlogFacts, ResearchNote  # type: ignore

from composer import Composer, SearchError
from schema import (CampaignLeadResult, CampaignResponse, CampaignTarget,
                    HubSpotOutcome, ResearchResponse, ResearchTarget)


def _crm_error(hubspot: HubSpotMCP, absent: str, refused: str) -> str:
    """Two different sentences for two different failures.

    A tool that refused because it could not read its HubSpot token is NOT a
    missing record — saying "not found" sends the reader looking for one that
    is sitting in the CRM the whole time.
    """
    detail = hubspot.last_error
    return f"crm-error: {refused} — {detail}" if detail else f"crm-error: {absent}"


def run_research(target: ResearchTarget, *,
                 settings: Settings | None = None,
                 obs: ResearchLogging | None = None,
                 hubspot: Optional[HubSpotMCP] = None,
                 composer: Optional[Composer] = None,
                 blog: Optional[BlogFacts] = None,
                 run_id: str = "") -> ResearchResponse:
    settings = settings or get_settings()
    obs = obs or get_obs(run_id or new_run_id(), refresh=True)
    hubspot = hubspot or HubSpotMCP(settings=settings, obs=obs)

    # The config belongs on the line that opens a STANDALONE run. In a campaign
    # these values were printed once by campaign_start; repeating ten of them
    # per lead is noise, not information.
    if blog is None:
        obs.process.emit("run_start", objectId=target.objectId,
                         summary_objectId=target.summary_objectId,
                         model=settings.model, mcp_url=settings.mcp_base_url,
                         write_tool=settings.mcp_tool_write,
                         property_name=settings.hubspot_context_property,
                         search_enabled=settings.search_enabled,
                         search_max_uses=settings.search_max_uses,
                         target_words=settings.note_target_words,
                         skip_if_context_present=settings.skip_if_context_present,
                         dry_run=settings.dry_run)

    def _failed(step: str, reason: str) -> ResearchResponse:
        obs.process.emit("run_failed", step=step, objectId=target.objectId, reason=reason)
        return ResearchResponse(run_id=obs.run_id, status="failed",
                                objectId=target.objectId, model=settings.model,
                                error=reason)

    # ---- step 1: the lead -------------------------------------------
    if not target.objectId:
        return _failed("input", "bad-data: no objectId was supplied, so there is "
                                "no lead to research")
    with obs.step("read_lead", objectId=target.objectId, via="mcp",
                  tool=settings.mcp_tool_read_lead, url=settings.mcp_base_url) as step:
        lead = hubspot.read_lead(target.objectId)
        if lead is None:
            reason = _crm_error(
                hubspot,
                f"the MCP returned no lead for objectId {target.objectId}",
                f"the MCP could not read the lead at objectId {target.objectId}")
            step.failed(reason, objectId=target.objectId)
            return _failed("read_lead", reason)
        step.ok(objectId=target.objectId, name=lead.display_name,
                company=lead.company, industry=lead.industry,
                job_title=lead.job_title, writable_missing=lead.writable or "none")

    # TEST AFFORDANCE (gap B1): a caller-supplied company name wins over the
    # MCP, which currently returns company_id but no name. Logged loudly so a
    # run using it is never mistaken for one where the MCP supplied the name.
    if target.company:
        obs.process.emit("company_override_applied", objectId=target.objectId,
                         supplied=target.company, from_mcp=lead.company,
                         reason="TEST ONLY — gap B1; the gateway never sends this")
        lead.company = target.company

    if settings.skip_if_context_present and lead.existing_lead_context.strip():
        obs.process.emit("run_skipped", objectId=target.objectId,
                         existing_context_chars=len(lead.existing_lead_context),
                         reason="lead_context already present and "
                                "LQABR_RESEARCH_SKIP_IF_CONTEXT_PRESENT=1")
        return ResearchResponse(
            run_id=obs.run_id, status="completed", objectId=target.objectId,
            lead=lead.to_dict(), model=settings.model,
            hubspot=HubSpotOutcome(status="skipped", objectId=target.objectId,
                                   property_name=settings.hubspot_context_property,
                                   tool=settings.mcp_tool_write,
                                   error="lead_context already present"))

    # ---- step 2: the published post ---------------------------------
    # `blog` is passed in by the campaign path, which has already read it once
    # — without that, a campaign over N leads would fetch the same post N times.
    if blog is None:
        # summary_objectId is the BLOG POST's record id. target.objectId is the
        # contact's — reading the blog by that would fetch the wrong record.
        if not target.summary_objectId:
            return _failed("input", "bad-data: no summary_objectId was supplied — the "
                                    "MCP reads the blog store by the post's record "
                                    "id, so the post cannot be fetched without it")
        with obs.step("read_blog", summary_objectId=target.summary_objectId, via="mcp",
                      tool=settings.mcp_tool_read_blog,
                      url=settings.mcp_base_url) as step:
            blog = hubspot.read_blog(target.summary_objectId)
            if blog is None or not blog.usable:
                reason = _crm_error(
                    hubspot,
                    f"no blog summary found for objectId {target.summary_objectId}",
                    f"the MCP could not read the blog post at objectId "
                    f"{target.summary_objectId}")
                step.failed(reason, objectId=target.objectId)
                return _failed("read_blog", reason)
            step.ok(objectId=target.objectId, blog_industry=blog.blog_industry,
                    summary_chars=len(blog.blog_summary), ticket_id=blog.ticket_id,
                    summary_preview=preview(blog.blog_summary))

    # ---- step 3: research + compose ---------------------------------
    composer = composer or Composer(settings=settings, obs=obs)
    with obs.step("research", objectId=target.objectId,
                  company=lead.company or "<not on the record>",
                  industry=lead.industry or blog.blog_industry,
                  model=settings.model, max_tokens=settings.max_tokens,
                  search_max_uses=settings.search_max_uses,
                  target_words=settings.note_target_words) as step:
        try:
            note: ResearchNote = composer.compose(lead, blog)
        except SearchError as exc:
            step.failed(str(exc), objectId=target.objectId)
            return _failed("research", str(exc))
        except Exception as exc:  # noqa: BLE001 - the step is named, not swallowed
            reason = f"the research pass failed ({type(exc).__name__}): {exc}"
            step.failed(reason, objectId=target.objectId)
            return _failed("research", reason)
        step.ok(objectId=target.objectId, chars=len(note.text),
                words=len(note.text.split()), sources=len(note.sources))

    # ---- step 4: write it back --------------------------------------
    with obs.step("write_context", objectId=target.objectId, via="mcp",
                  tool=settings.mcp_tool_write,
                  property_name=settings.hubspot_context_property,
                  chars=len(note.as_hubspot_text(settings.note_max_chars)),
                  dry_run=settings.dry_run) as step:
        outcome = hubspot.write_context(lead, note)
        written = {"objectId": target.objectId,
                   "write_status": outcome.status, "chars": outcome.chars}
        if outcome.status in ("skipped", "dry_run"):
            # `ok` on the WriteResult (nothing needed doing) but not an `ok`
            # step — say which it was.
            step.skipped(outcome.error or outcome.status, **written)
        elif outcome.ok:
            step.ok(**written)
        else:
            step.failed(outcome.error, **written)

    response = ResearchResponse(
        run_id=obs.run_id,
        # status follows the WRITE: a note that could not be landed is a failure,
        # and the note still comes back so the work is not lost.
        status="completed" if outcome.ok else "failed",
        objectId=target.objectId,
        lead=lead.to_dict(),
        blog=blog.to_dict(),
        note=note.text,
        sources=note.sources,
        searches=note.searches,
        hubspot=HubSpotOutcome(**outcome.to_dict()),
        model=settings.model,
        error="" if outcome.ok else outcome.error,
    )
    obs.process.emit("run_complete", status=response.status,
                     write_status=outcome.status, objectId=target.objectId,
                     chars=outcome.chars, sources=len(note.sources))
    return response


def run_campaign(target: CampaignTarget, *,
                 settings: Settings | None = None,
                 obs: ResearchLogging | None = None,
                 hubspot: Optional[HubSpotMCP] = None,
                 composer: Optional[Composer] = None,
                 run_id: str = "") -> CampaignResponse:
    """One published post -> every lead in its industry.

        read the post        (MCP, by its objectId)
        take its industry    (blog_industry, off the post)
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

    obs.process.emit("campaign_start", objectId=target.objectId,
                     limit=target.limit,
                     model=settings.model, mcp_url=settings.mcp_base_url,
                     lead_lookup="hubspot_direct" if settings.use_direct_lead_lookup
                                 else settings.mcp_tool_list_leads,
                     property_name=settings.hubspot_context_property,
                     skip_if_context_present=settings.skip_if_context_present,
                     dry_run=settings.dry_run)

    def _failed(step: str, reason: str, **extra) -> CampaignResponse:
        obs.process.emit("campaign_failed", step=step,
                         objectId=target.objectId, reason=reason)
        return CampaignResponse(run_id=obs.run_id, status="failed",
                                objectId=target.objectId,
                                model=settings.model, error=reason, **extra)

    # ---- step 1: the post --------------------------------------------
    # The MCP reads the blog store by the Ticket's own record id, which is
    # exactly what the gateway's blog-summary route hands over.
    if not target.objectId:
        return _failed("input",
                       "bad-data: no objectId was supplied, so there is no "
                       "blog post to build a campaign from")
    with obs.step("read_blog", objectId=target.objectId, via="mcp",
                  tool=settings.mcp_tool_read_blog, url=settings.mcp_base_url,
                  detail="the objectId on this route is a blog POST") as step:
        blog = hubspot.read_blog(target.objectId)
        if blog is None or not blog.usable:
            reason = _crm_error(
                hubspot,
                f"no blog summary found for objectId {target.objectId}",
                f"the MCP could not read the blog post at objectId "
                f"{target.objectId}")
            step.failed(reason)
            return _failed("read_blog", reason)
        step.ok(objectId=target.objectId, blog_industry=blog.blog_industry,
                summary_chars=len(blog.blog_summary), ticket_id=blog.ticket_id,
                summary_preview=preview(blog.blog_summary))

    industry = (blog.blog_industry or "").strip()
    if not industry:
        return _failed("industry",
                       "bad-data: the post carries no blog_industry, so there "
                       "is no industry to select leads by",
                       blog=blog.to_dict())

    # ---- step 2: who is in that industry -----------------------------
    with obs.step("list_leads", industry=industry, limit=target.limit,
                  via="hubspot_direct" if settings.use_direct_lead_lookup else "mcp",
                  tool="" if settings.use_direct_lead_lookup
                       else settings.mcp_tool_list_leads) as step:
        lead_ids = hubspot.list_leads_by_industry(industry, limit=target.limit)
        if lead_ids is None:
            # "could not ask" is NOT "nobody matched" — refuse rather than
            # report an empty campaign that silently skipped every lead.
            step.failed("the industry lookup could not be made — not the same as "
                        "nobody matching, so the campaign refuses to run")
            return _failed("list_leads",
                           f"crm-error: could not list leads for industry "
                           f"{industry!r} — the campaign is not run rather than "
                           "run against an unknown set of leads",
                           industry=industry, blog=blog.to_dict())
        step.ok(industry=industry, leads_found=len(lead_ids), leads=lead_ids[:20])

    if not lead_ids:
        # A real, valid answer: nobody matched. Not a failure.
        obs.process.emit("campaign_complete", objectId=target.objectId,
                         status="completed", industry=industry, leads_found=0,
                         written=0, failed=0, skipped=0,
                         reason="no lead is in this industry")
        return CampaignResponse(
            run_id=obs.run_id, status="completed", objectId=target.objectId,
            industry=industry, leads_found=0, blog=blog.to_dict(),
            model=settings.model)

    # ---- step 3: one lead at a time ----------------------------------
    # The blog is passed down already-read: a campaign over N leads must not
    # fetch the same post N times.
    composer = composer or Composer(settings=settings, obs=obs)
    results: List[CampaignLeadResult] = []
    for position, leadObjectId in enumerate(lead_ids, start=1):
        obs.process.emit("campaign_lead_start", objectId=leadObjectId,
                         position=position, of=len(lead_ids), industry=industry)
        one = run_research(
            ResearchTarget(objectId=leadObjectId,
                           summary_objectId=target.objectId),
            settings=settings, obs=obs, hubspot=hubspot,
            composer=composer, blog=blog, run_id=obs.run_id)
        write_status = (one.hubspot.status if one.hubspot else "") or one.status
        result = CampaignLeadResult(
            objectId=leadObjectId,
            status="skipped" if write_status == "skipped" else one.status,
            chars=one.hubspot.chars if one.hubspot else 0,
            error=one.error)
        results.append(result)
        obs.process.emit("campaign_lead_done", objectId=leadObjectId,
                         position=position, of=len(lead_ids),
                         status=result.status, chars=result.chars,
                         error=result.error[:200])

    written = sum(1 for r in results if r.status == "completed")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    status = ("completed" if failed == 0
              else "failed" if written == 0 and skipped == 0
              else "partial")

    obs.process.emit("campaign_complete", objectId=target.objectId,
                     status=status, industry=industry,
                     leads_found=len(lead_ids), written=written,
                     failed=failed, skipped=skipped,
                     failed_leads=[r.objectId for r in results
                                   if r.status == "failed"][:20])
    return CampaignResponse(
        run_id=obs.run_id, status=status, objectId=target.objectId,
        industry=industry, leads_found=len(lead_ids), written=written,
        failed=failed, skipped=skipped, results=results, blog=blog.to_dict(),
        model=settings.model,
        error="" if status == "completed" else f"{failed} of {len(lead_ids)} leads failed")
