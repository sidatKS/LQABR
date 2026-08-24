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
    blog_published_at: str = ""    # the MCP's key into the blog store
    summary_ref_id: str = ""       # the blog Ticket id (correlation only)
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
    blog_published_at: str = ""
    company: str = ""              # test affordance — see ResearchTarget.company

    def resolved(self) -> ResearchTarget:
        target = self.target or ResearchTarget()
        return ResearchTarget(
            object_id=(target.object_id or self.object_id or "").strip(),
            blog_published_at=(target.blog_published_at
                               or self.blog_published_at or "").strip(),
            summary_ref_id=target.summary_ref_id or "",
            company=(target.company or self.company or "").strip(),
        )


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

    The ids live in ``params.metadata`` (``object_id``, ``blog_published_at``,
    ``summary_ref_id``); the gateway also mirrors ``object_id`` at the top level
    for agents that have not caught up, so both are read.
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
            blog_published_at=str(meta.get("blog_published_at") or "").strip(),
            summary_ref_id=str(meta.get("summary_ref_id") or "").strip(),
        )

    def run_id(self) -> str:
        return str(self._meta().get("run_id") or "").strip()
