"""The HTTP contract — request in, response out.

Pydantic at the edge only; everything inside the agent moves as the dataclasses
in ``research_core.types``. Keeping the boundary types here means a change to
the wire format never reaches the pipeline.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

#: camelCase -> snake_case. The wire spells things HubSpot's way; this agent
#: spells them one way. The translation happens at the edge, in `_meta()` and
#: the aliases on `A2AEnvelope` — nowhere else.
_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


def _snake(name: str) -> str:
    return _SNAKE.sub("_", str(name)).lower()


#: HubSpot's object type -> what this agent calls the record.
_RECORD_KINDS = {"contact": "contact", "ticket": "post"}


def _wire(*spellings: str, default: Any = None) -> Any:
    """A field that arrives under any of these names and lives under ours."""
    return Field(default=default, validation_alias=AliasChoices(*spellings))


#: Requests may still spell either id the old way. Inside, and on the way out, it
#: is `objectId` — but a documented curl must not stop working because we
#: renamed something.
_ID = ("objectId", "object_id")
#: `summary_ref_id` never said WHAT it referenced. It is the blog post's
#: record id, so inside it is `summary_objectId` — the same `objectId` token as
#: the contact's, which is the point. The gateway and every existing caller
#: still say the old thing, and both keep working.
_BLOG_ID = ("summary_objectId", "summary_object_id",
            "summary_ref_id", "summaryRefId")
_ACCEPTS_BOTH = ConfigDict(populate_by_name=True)


class ResearchTarget(BaseModel):
    """Which lead, and which post. Both ids come from the gateway's dispatch."""

    model_config = _ACCEPTS_BOTH

    objectId: str = _wire(*_ID, default="")   # the HubSpot CONTACT record id
    summary_objectId: str = _wire(*_BLOG_ID, default="")
                                 # the BLOG POST's record id — the MCP reads
                                   # the blog store by it. A different record
                                   # from objectId; swapping them reads the
                                   # wrong row.
    #: TEST AFFORDANCE ONLY (gap B1). The MCP's get_lead_profile returns
    #: company_id but not the company NAME, so company-specific research cannot
    #: be exercised end to end yet. Supplying this overrides the MCP value for
    #: one run. The GATEWAY NEVER SENDS IT — the A2A path stays honest — and it
    #: must be removed once the MCP returns the name.
    company: str = ""


class CampaignTarget(BaseModel):
    """One published post, fanned out over every lead in its industry.

    `objectId` is the blog post's record id — exactly what the gateway's
    blog-summary route hands over, and exactly what the MCP's
    get_blog_summary takes. There is no `industry` here: it belongs to the
    post and is read off it in run_campaign.
    """

    model_config = _ACCEPTS_BOTH

    objectId: str = _wire(*_ID, default="")
    limit: int = 100


class CampaignLeadResult(BaseModel):
    """One lead's outcome inside a campaign. A failure here never stops the
    others — it is reported with its reason and the campaign continues."""

    objectId: str = ""
    status: str = ""        # completed | failed | skipped
    chars: int = 0
    error: str = ""


class CampaignResponse(BaseModel):
    run_id: str = ""
    status: str = "completed"   # completed | partial | failed
    objectId: str = ""         # the blog post this campaign ran from
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
    objectId: str = ""
    property_name: str = ""
    chars: int = 0
    tool: str = ""
    error: str = ""


class ResearchResponse(BaseModel):
    run_id: str = ""
    status: str = "completed"           # completed | failed
    objectId: str = ""
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

    **One spelling inside.** HubSpot names its event fields in camelCase and
    the gateway forwards the event verbatim — at the top level, or nested in
    ``params.metadata``, which is authoritative. Both spellings are accepted
    HERE and nowhere else: the aliases below and ``_meta()`` normalise on the
    way in, so every other line of this agent, and every log field, reads
    ``objectId``.
    """

    model_config = ConfigDict(populate_by_name=True)

    #: Defaults to "2.0", so EVERY caller is answered in the JSON-RPC shape,
    #: including a bare HubSpot webhook that never sent the field. That is
    #: deliberate and tested (`test_a_contact_event_is_refused_at_the_door`
    #: reads `["result"]` off a bare webhook). It also means the
    #: `jsonrpc == "2.0"` branches in `_accept`/`_rejected` are unreachable
    #: today — they are kept symmetric so the two paths cannot drift, but
    #: turning them on is a change to the ACK shape, not a tidy-up.
    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    method: str = ""
    params: Optional[Dict[str, Any]] = None

    #: Mirrored at the top level by the gateway's compat shim, for agents that
    #: are plain REST rather than A2A. Metadata wins when both are present.
    objectId: Optional[str] = _wire("object_id", "objectId")
    summary_objectId: Optional[str] = _wire(*_BLOG_ID)

    #: HubSpot's own event fields. None of them are needed to RUN — the id is —
    #: but `subscription_type` says which record kind arrived, and that is the
    #: one mix-up this agent cannot recover from: a Ticket sent to the contact
    #: route fails at read_lead with a CRM error that reads like a missing
    #: record. `attempt_number` marks a redelivery, which otherwise looks like
    #: a duplicate campaign nobody asked for.
    subscription_type: Optional[str] = _wire("subscription_type", "subscriptionType")
    property_name: Optional[str] = _wire("property_name", "propertyName")
    event_id: Optional[Any] = _wire("event_id", "eventId")
    attempt_number: Optional[int] = _wire("attempt_number", "attemptNumber")

    def _meta(self) -> Dict[str, Any]:
        """The metadata, with every key in ONE spelling."""
        raw = (self.params or {}).get("metadata") or {}
        return {_snake(key): value for key, value in raw.items()}

    @staticmethod
    def _int(value: Any, default: int) -> int:
        """A number off the wire, or the default. HubSpot's webhook fields
        arrive as whatever HubSpot sends and the gateway forwards them
        verbatim — `limit: "all"` used to be a 500 from a route whose whole job
        is to reject a bad payload with a named reason."""
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _first(*candidates: Any) -> str:
        for candidate in candidates:
            if candidate and str(candidate).strip():
                return str(candidate).strip()
        return ""

    def target(self) -> ResearchTarget:
        return ResearchTarget(
            objectId=self._first(self._meta().get("object_id"),
                                  self.objectId),
            # Read the same ways as objectId. An id that resolves one way and
            # not the other is a trap waiting for the next caller.
            summary_objectId=self._first(self._meta().get("summary_object_id"),
                                         self._meta().get("summary_ref_id"),
                                         self.summary_objectId),
        )

    def campaign_target(self) -> CampaignTarget:
        """The same envelope read as a POST, not a contact.

        On the blog-summary route the gateway's `objectId` IS the published
        post — so it maps to CampaignTarget.objectId, never to a contact id.
        `limit` is an optional override for a hand-driven re-run. The
        industry is never taken from the envelope: the gateway cannot send one
        (it is absent from the gateway's ALLOWED_METADATA_KEYS, and an unlisted
        key makes the dispatch raise), and it belongs to the post regardless.
        """
        return CampaignTarget(
            objectId=self.target().objectId,
            # Bounded as well as parsed: `limit=0` used to make the lookup
            # return [] with no HTTP call at all, and the campaign then reported
            # a clean "no lead is in this industry".
            limit=max(1, min(1000, self._int(self._meta().get("limit"), 100))),
        )

    def run_id(self) -> str:
        return str(self._meta().get("run_id") or "").strip()

    def source(self) -> Dict[str, Any]:
        """What HubSpot said about this event, for the log. Absent fields stay
        out rather than printing as empty columns."""
        facts: Dict[str, Any] = {}
        for value in (self._meta().get("attempt_number"), self.attempt_number):
            # `0` is the common case and falsy, so test for None, not truth —
            # and only report a RE-delivery, which is the interesting one.
            if value is not None:
                attempt = self._int(value, 0)
                if attempt > 0:
                    facts["attempt"] = attempt
                break
        facts.update({
            "subscription_type": self._first(self._meta().get("subscription_type"),
                                             self.subscription_type),
            "property_name": self._first(self._meta().get("property_name"),
                                         self.property_name),
            "event_id": self._first(self._meta().get("event_id"), self.event_id),
        })
        return {key: value for key, value in facts.items() if value != ""}

    def record_kind(self) -> str:
        """`contact` / `post` / `""` when the event does not say."""
        prefix = self.source().get("subscription_type", "").split(".", 1)[0]
        return _RECORD_KINDS.get(prefix.lower(), "")
