"""What this agent asks the HubSpot MCP to do — one method per operation.

Everything HubSpot-shaped is confined to this file. If the container's tool
names, argument names or property names ever change, this is the only module
that moves, and most of the time the change is an environment variable rather
than an edit here.

Three operations, all through the MCP, none direct to HubSpot:

    read_lead(objectId)              the contact: industry, company, and the
                                      three ids the write tool demands back
    read_blog(objectId)              the published post, by its record id
    write_context(lead, note)         the lead_context write-back

The write is the point of the agent: a lead_context landing on the contact is
what the gateway's R2-lead-context route is waiting for. A write that fails is
reported as a failure — never smoothed into a success.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..obs import Observability, get_obs, preview
from ..settings import Settings, get_settings
from ..types import BlogFacts, LeadFacts, ResearchNote, WriteResult
from .client import MCPClient, MCPError


def _first(data: Dict[str, Any], *names: str) -> str:
    """First non-empty value among `names`. The MCP's field naming has drifted
    across revisions (company vs company_name), so read defensively rather than
    pinning one spelling."""
    for name in names:
        value = data.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _flatten(payload: Any) -> Dict[str, Any]:
    """The tool result, whether the fields are at the top level or nested under
    a wrapper key ('lead', 'profile', 'summary')."""
    if not isinstance(payload, dict):
        return {}
    flat: Dict[str, Any] = dict(payload)
    for key in ("lead", "profile", "summary", "result", "data"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            flat.update(inner)
    return flat


#: A reply that says the tool did not run, as opposed to running and finding
#: nothing. The MCP reports both as a body, so they are told apart by shape.
_REFUSAL_STATUSES = ("halted", "failed", "error")


def refusal(flat: Dict[str, Any]) -> str:
    """The reason a read did not happen, or "" if it simply found nothing.

    `{"found": false}` is a real answer: that record does not exist. But
    `{"found": false, "status": "halted", "failure_kind": "systemic",
      "reasons": ["AuthConfigError: could not read .../lqabr-hubspot-access-token"]}`
    is the MCP saying it could never ask — usually because IT could not read its
    own HubSpot token. Reporting that as "no blog summary found" sends whoever
    is on the other end looking for a missing record that is sitting right
    there. Same rule as the industry lookup: "could not ask" is not "nobody
    matched".
    """
    status = str(flat.get("status", "")).lower()
    if not (flat.get("failure_kind") or flat.get("error")
            or status in _REFUSAL_STATUSES):
        return ""
    return (str(flat.get("error") or "")
            or "; ".join(str(r) for r in (flat.get("reasons") or []))
            or status or "the MCP refused the read")


class HubSpotMCP:
    """The HubSpot MCP, as this agent uses it."""

    def __init__(self, client: MCPClient | None = None, *,
                 settings: Settings | None = None,
                 obs: Observability | None = None) -> None:
        self._settings = settings or get_settings()
        self._obs = obs or get_obs()
        self._client = client or MCPClient(self._settings, obs=self._obs)
        self._last_error = ""

    @property
    def client(self) -> MCPClient:
        return self._client

    @property
    def last_error(self) -> str:
        """Why the most recent read returned None. Empty when it simply found
        nothing. The reads keep their `Optional[...]` contract; the pipeline
        reads this so the failure it reports names the real cause."""
        return self._last_error

    def ensure_ready(self) -> List[str]:
        """Startup check — the three tools we actually call must exist.

        The lead-listing tool is deliberately NOT asserted: it does not exist
        on the MCP yet, and the campaign reaches HubSpot directly for that one
        lookup (see hubspot_direct.py). Asserting it would refuse to start over
        a tool nothing calls.
        """
        return self._client.ensure_ready([self._settings.mcp_tool_read_lead,
                                          self._settings.mcp_tool_read_blog,
                                          self._settings.mcp_tool_write])

    # -------------------------------------------------------------- reads
    def read_lead(self, objectId: str) -> Optional[LeadFacts]:
        """The contact behind this hand-off. None when the MCP has nothing —
        the caller reports `bad-data`, it never invents a lead."""
        settings = self._settings
        self._last_error = ""
        try:
            result = self._client.call_tool(
                settings.mcp_tool_read_lead,
                {settings.mcp_object_id_arg: str(objectId)})
        except MCPError as exc:
            self._last_error = str(exc)
            self._obs.process.emit("lead_read_failed", objectId=objectId, reason=str(exc))
            return None

        flat = _flatten(result)
        refused = refusal(flat)
        if refused:
            self._last_error = refused
            self._obs.process.emit("lead_read_rejected", objectId=objectId,
                                   tool=settings.mcp_tool_read_lead, reason=refused)
            return None
        if not flat or flat.get("found") is False:
            self._obs.process.emit("lead_read_empty", objectId=objectId,
                                   reason="not found")
            return None

        lead = LeadFacts(
            objectId=str(objectId),
            first_name=_first(flat, "first_name", "firstname"),
            last_name=_first(flat, "last_name", "lastname"),
            job_title=_first(flat, "job_title", "jobtitle"),
            industry=_first(flat, "industry"),
            # NOT company_id: an internal identifier is not a name. Searching
            # for "C0017" returned the MITRE ATT&CK campaign of that name and a
            # city procurement dataset — garbage in, garbage research out. An
            # unknown company is better handled explicitly by the prompt.
            company=_first(flat, "company", "company_name"),
            company_about=_first(flat, "company_about", "about_us"),
            # `website_url` is what the MCP actually sends (verified against the
            # live container 2026-08-25). Reading three other spellings and not
            # that one meant the company URL never reached the prompt, so every
            # note was researched from the company NAME alone.
            company_website=_first(flat, "company_website", "website_url",
                                   "website", "domain"),
            employee_id=_first(flat, "employee_id"),
            company_id=_first(flat, "company_id"),
            decision_maker_flag=_first(flat, "decision_maker_flag", "decision_maker"),
            existing_lead_context=_first(flat, settings.hubspot_context_property,
                                         "lead_context"),
        )
        return lead

    def list_leads_by_industry(self, industry: str,
                               limit: int = 100) -> Optional[List[str]]:
        """Every lead in one industry — the campaign fan-out.

        Returns contact objectIds, or None when the lookup could not be made
        (tool absent, MCP down, bad reply). None and [] mean different things:
        None is "we could not ask", [] is "asked, nobody matched" — the caller
        reports them differently and never treats a failure as an empty
        campaign.
        """
        settings = self._settings
        industry = (industry or "").strip()
        if not industry:
            self._obs.process.emit("leads_list_skipped",
                                   reason="bad-data: no industry to match on")
            return None

        # The one read that bypasses the MCP, because the MCP has no
        # lead-listing tool. Scoped to this lookup and removable in one config
        # flip — see research_core/hubspot_direct.py.
        if settings.use_direct_lead_lookup:
            try:
                from ..hubspot_direct import HubSpotDirect
            except ImportError:  # pragma: no cover
                from hubspot_direct import HubSpotDirect  # type: ignore
            try:
                ids = HubSpotDirect(settings=settings,
                                    obs=self._obs).list_leads_by_industry(
                                        industry, limit=limit)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                self._obs.process.emit("leads_list_failed", industry=industry,
                                       source="hubspot_direct", reason=str(exc))
                return None
            return ids

        try:
            result = self._client.call_tool(settings.mcp_tool_list_leads,
                                            {"industry": industry, "limit": limit})
        except MCPError as exc:
            self._obs.process.emit("leads_list_failed", industry=industry,
                                   tool=settings.mcp_tool_list_leads, reason=str(exc))
            return None

        flat = _flatten(result)
        if flat.get("error") or flat.get("failure_kind"):
            self._obs.process.emit("leads_list_rejected", industry=industry,
                                   reason=str(flat.get("error") or flat.get("failure_kind")))
            return None

        rows = result if isinstance(result, list) else (
            flat.get("leads") or flat.get("results") or flat.get("contacts") or [])
        if not isinstance(rows, list):
            self._obs.process.emit("leads_list_rejected", industry=industry,
                                   reason=f"unexpected reply shape: {type(rows).__name__}")
            return None

        ids: List[str] = []
        for row in rows:
            if isinstance(row, str):
                ids.append(row.strip())
                continue
            if isinstance(row, dict):
                found = _first(row, "objectId", "object_id", "contact_hs_id",
                               "contact_id", "id")
                if found:
                    ids.append(str(found).strip())
        ids = [i for i in ids if i]
        return ids

    def read_blog(self, objectId: str) -> Optional[BlogFacts]:
        """The post that triggered the run, read by its Ticket record id.

        The MCP keys the blog store on the ticket's objectId (changed
        2026-08-24 from the publication timestamp), which is exactly what the
        gateway's blog-summary route hands over — so the ticket id fetches the
        post with no timestamp round-trip.
        """
        settings = self._settings
        self._last_error = ""
        try:
            result = self._client.call_tool(
                settings.mcp_tool_read_blog,
                {settings.mcp_object_id_arg: str(objectId)})
        except MCPError as exc:
            self._last_error = str(exc)
            self._obs.process.emit("blog_read_failed",
                                   objectId=objectId, reason=str(exc))
            return None

        flat = _flatten(result)
        # The MCP could not ask. Not the same as asking and finding nothing.
        refused = refusal(flat)
        if refused:
            self._last_error = refused
            self._obs.process.emit("blog_read_rejected", objectId=objectId,
                                   tool=settings.mcp_tool_read_blog, reason=refused)
            return None
        # Not-found is a VALID result on this tool, never an error.
        if not flat or flat.get("found") is False:
            self._obs.process.emit("blog_read_empty", objectId=objectId)
            return None

        blog = BlogFacts(
            blog_published_at=_first(flat, "blog_published_at"),
            blog_summary=_first(flat, "blog_summary"),
            blog_industry=_first(flat, "blog_industry"),
            ticket_id=_first(flat, "ticket_hs_id", "ticket_id") or str(objectId),
        )
        warnings = flat.get("warnings")
        if warnings:
            self._obs.process.emit("blog_read_warnings",
                                   objectId=objectId, warnings=warnings)
        return blog

    # -------------------------------------------------------------- write
    def write_context(self, lead: LeadFacts, note: ResearchNote) -> WriteResult:
        """Land the note on the lead-context property, through the MCP.

        The write tool REQUIRES employee_id + company_id + decision_maker_flag,
        so a lead missing any of them is reported as `bad-data` and skipped —
        flagged, never dropped silently.
        """
        settings = self._settings
        prop = settings.hubspot_context_property
        text = note.as_hubspot_text(settings.note_max_chars)

        if not text.strip():
            self._obs.process.emit("context_write_not_writable", objectId=lead.objectId,
                                   reason="bad-data: the composed note was empty")
            return WriteResult(status="not_writable", objectId=lead.objectId,
                               property_name=prop, tool=settings.mcp_tool_write,
                               error="bad-data: the composed note was empty")

        missing = lead.writable
        if missing:
            reason = (f"bad-data: lead is missing {missing}, which "
                      f"{settings.mcp_tool_write} requires to write")
            self._obs.process.emit("context_write_not_writable", objectId=lead.objectId,
                                   reason=reason)
            return WriteResult(status="not_writable", objectId=lead.objectId,
                               property_name=prop, tool=settings.mcp_tool_write,
                               error=reason)

        if settings.dry_run:
            self._obs.process.emit("context_write_dry_run", objectId=lead.objectId,
                                   tool=settings.mcp_tool_write,
                                   property_name=prop, chars=len(text),
                                   reason="LQABR_RESEARCH_DRY_RUN=1 — composed and "
                                          "logged, deliberately not sent",
                                   text_preview=preview(text))
            return WriteResult(status="dry_run", objectId=lead.objectId,
                               property_name=prop, chars=len(text),
                               tool=settings.mcp_tool_write)

        arguments = {
            "employee_id": lead.employee_id,
            "company_id": lead.company_id,
            "decision_maker_flag": lead.decision_maker_flag,
            prop: text,
        }
        try:
            result = self._client.call_tool(settings.mcp_tool_write, arguments)
        except MCPError as exc:
            self._obs.process.emit("context_write_failed", objectId=lead.objectId,
                                   tool=settings.mcp_tool_write, reason=str(exc))
            return WriteResult(status="error", objectId=lead.objectId,
                               property_name=prop, chars=len(text),
                               tool=settings.mcp_tool_write, error=str(exc))

        # A rejected write is reported as a BODY, not by raising:
        #   {"error": ...}                              a classic rejection
        #   {"status": "halted"/"failed", "reasons":[]} a systemic failure
        #      (e.g. the MCP could not read its HubSpot token)
        # Treat all of them as failures — a non-write never reads as written.
        if isinstance(result, dict):
            status_value = str(result.get("status", "")).lower()
            if (result.get("error") or result.get("failure_kind")
                    or status_value in ("halted", "failed", "error")):
                reason = (str(result.get("error") or "")
                          or "; ".join(str(r) for r in (result.get("reasons") or []))
                          or status_value or "write rejected")
                self._obs.process.emit("context_write_rejected", objectId=lead.objectId,
                                       tool=settings.mcp_tool_write, reason=reason)
                return WriteResult(status="error", objectId=lead.objectId,
                                   property_name=prop, chars=len(text),
                                   tool=settings.mcp_tool_write, error=reason)

        # (This write raises HubSpot trigger 2 — contact.propertyChange on
        # lead_context — which the gateway routes to the Email agent. The
        # outcome is reported by `step_out write_context`.)
        return WriteResult(status="written", objectId=lead.objectId,
                           property_name=prop, chars=len(text),
                           tool=settings.mcp_tool_write)
