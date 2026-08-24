"""The HTTP contract — request in, response out.

Pydantic at the edge only; everything inside the agent moves as the dataclasses
in ``research_core.types``. Keeping the boundary types here means a change to
the wire format never reaches the pipeline.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ResearchTarget(BaseModel):
    """Which lead, and which post. Both ids come from the gateway's dispatch."""

    object_id: str = ""            # the HubSpot CONTACT record id
    summary_ref_id: str = ""       # the BLOG POST's record id — the MCP reads
                                   # the blog store by it. A different record
                                   # from object_id; swapping them reads the
                                   # wrong row.
    #: TEST AFFORDANCE ONLY (gap B1). The MCP's get_lead_profile returns
    #: company_id but not the company NAME, so company-specific research cannot
    #: be exercised end to end yet. Supplying this overrides the MCP value for
    #: one run. The GATEWAY NEVER SENDS IT — the A2A path stays honest — and it
    #: must be removed once the MCP returns the name.
    company: str = ""


class ResearchRequest(BaseModel):
    """POST /research/run."""

    target: Optional[ResearchTarget] = None
    #: Convenience mirrors so a hand-written curl need not nest.
    object_id: str = ""
    summary_ref_id: str = ""
    company: str = ""              # test affordance — see ResearchTarget.company

    def resolved(self) -> ResearchTarget:
        target = self.target or ResearchTarget()
        return ResearchTarget(
            object_id=(target.object_id or self.object_id or "").strip(),
            summary_ref_id=(target.summary_ref_id
                            or self.summary_ref_id or "").strip(),
            company=(target.company or self.company or "").strip(),
        )


class CampaignTarget(BaseModel):
    """One published post, fanned out over every lead in its industry.

    `object_id` is the blog post's record id — exactly what the gateway's
    blog-summary route hands over, and exactly what the MCP's
    get_blog_summary takes. `industry` normally comes FROM the post;
    supplying it here overrides that, for a re-run against one industry.
    """

    object_id: str = ""
    industry: str = ""
    limit: int = 100


class CampaignRequest(BaseModel):
    """POST /research/campaign."""

    target: Optional[CampaignTarget] = None
    object_id: str = ""
    industry: str = ""
    limit: int = 100

    def resolved(self) -> CampaignTarget:
        t = self.target or CampaignTarget()
        return CampaignTarget(
            object_id=(t.object_id or self.object_id or "").strip(),
            industry=(t.industry or self.industry or "").strip(),
            limit=t.limit or self.limit or 100,
        )


class CampaignLeadResult(BaseModel):
    """One lead's outcome inside a campaign. A failure here never stops the
    others — it is reported with its reason and the campaign continues."""

    object_id: str = ""
    status: str = ""        # completed | failed | skipped
    chars: int = 0
    error: str = ""


class CampaignResponse(BaseModel):
    run_id: str = ""
    status: str = "completed"   # completed | partial | failed
    object_id: str = ""         # the blog post this campaign ran from
    industry: str = ""
    #: How many leads matched the industry — the count asked for before any
    #: context is written.
    leads_found: int = 0
    written: int = 0
    failed: int = 0
    skipped: int = 0
    results: List[CampaignLeadResult] = Field(default_factory=list)
    blog: Dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    error: str = ""


class HubSpotOutcome(BaseModel):
    status: str = ""
    object_id: str = ""
    property_name: str = ""
    chars: int = 0
    tool: str = ""
    error: str = ""


class ResearchResponse(BaseModel):
    run_id: str = ""
    status: str = "completed"           # completed | failed
    object_id: str = ""
    lead: Dict[str, Any] = Field(default_factory=dict)
    blog: Dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    sources: List[str] = Field(default_factory=list)
    searches: int = 0
    hubspot: Optional[HubSpotOutcome] = None
    model: str = ""
    error: str = ""


class A2AEnvelope(BaseModel):
    """The gateway's JSON-RPC ``message/send`` envelope.

    The ids live in ``params.metadata`` (``object_id``, ``summary_ref_id``);
    the gateway also mirrors ``object_id`` at the top level for agents that
    have not caught up, so both are read.
    """

    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    method: str = ""
    params: Optional[Dict[str, Any]] = None
    object_id: Optional[str] = None
    objectId: Optional[str] = None  # noqa: N815 - the wire spells it this way

    def _meta(self) -> Dict[str, Any]:
        return ((self.params or {}).get("metadata") or {})

    def target(self) -> ResearchTarget:
        meta = self._meta()
        object_id = ""
        for candidate in (meta.get("object_id"), self.object_id, self.objectId):
            if candidate and str(candidate).strip():
                object_id = str(candidate).strip()
                break
        return ResearchTarget(
            object_id=object_id,
            summary_ref_id=str(meta.get("summary_ref_id") or "").strip(),
        )

    def campaign_target(self) -> CampaignTarget:
        """The same envelope read as a POST, not a contact.

        On the blog-summary route the gateway's `object_id` IS the published
        post — so it maps to CampaignTarget.object_id, never to a contact id.
        `industry` and `limit` are optional overrides for a hand-driven re-run;
        normally the industry comes off the post itself.
        """
        meta = self._meta()
        return CampaignTarget(
            object_id=self.target().object_id,
            industry=str(meta.get("industry") or "").strip(),
            limit=int(meta.get("limit") or 100),
        )

    def run_id(self) -> str:
        return str(self._meta().get("run_id") or "").strip()
