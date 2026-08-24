"""What this agent asks the HubSpot MCP to do — one method per operation.

Everything HubSpot-shaped is confined to this file. If the container's tool
names, argument names or property names ever change, this is the only module
that moves, and most of the time the change is an environment variable rather
than an edit here.

Three operations, all through the MCP, none direct to HubSpot:

    read_lead(object_id)              the contact: industry, company, and the
                                      three ids the write tool demands back
    read_blog(blog_published_at)      the published post that triggered the run
    write_context(lead, note)         the lead_context write-back

The write is the point of the agent: a lead_context landing on the contact is
what the gateway's R2-lead-context route is waiting for. A write that fails is
reported as a failure — never smoothed into a success.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..obs import Observability, get_obs
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


class HubSpotMCP:
    """The HubSpot MCP, as this agent uses it."""

    def __init__(self, client: MCPClient | None = None, *,
                 settings: Settings | None = None,
                 obs: Observability | None = None) -> None:
        self._settings = settings or get_settings()
        self._obs = obs or get_obs()
        self._client = client or MCPClient(self._settings, obs=self._obs)

    @property
    def client(self) -> MCPClient:
        return self._client

    def ensure_ready(self) -> List[str]:
        """Startup check — the three tools we are configured to use must exist."""
        return self._client.ensure_ready([
            self._settings.mcp_tool_read_lead,
            self._settings.mcp_tool_read_blog,
            self._settings.mcp_tool_write,
        ])

    # -------------------------------------------------------------- reads
    def read_lead(self, object_id: str) -> Optional[LeadFacts]:
        """The contact behind this hand-off. None when the MCP has nothing —
        the caller reports `bad-data`, it never invents a lead."""
        settings = self._settings
        try:
            result = self._client.call_tool(settings.mcp_tool_read_lead,
                                            {"object_id": str(object_id)})
        except MCPError as exc:
            self._obs.process.emit("lead_read_failed", object_id=object_id, reason=str(exc))
            return None

        flat = _flatten(result)
        if not flat or flat.get("found") is False or flat.get("error"):
            self._obs.process.emit("lead_read_empty", object_id=object_id,
                                   reason=str(flat.get("error") or "not found"))
            return None

        lead = LeadFacts(
            object_id=str(object_id),
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
            company_website=_first(flat, "company_website", "website", "domain"),
            employee_id=_first(flat, "employee_id"),
            company_id=_first(flat, "company_id"),
            decision_maker_flag=_first(flat, "decision_maker_flag", "decision_maker"),
            existing_lead_context=_first(flat, settings.hubspot_context_property,
                                         "lead_context"),
        )
        self._obs.process.emit("lead_read_ok", object_id=object_id,
                               industry=lead.industry, company=lead.company,
                               has_existing_context=bool(lead.existing_lead_context))
        return lead

    def read_blog(self, blog_published_at: str) -> Optional[BlogFacts]:
        """The post that triggered the run, keyed on its publication timestamp —
        which is how the central MCP indexes the blog store."""
        settings = self._settings
        try:
            result = self._client.call_tool(settings.mcp_tool_read_blog,
                                            {"blog_published_at": str(blog_published_at)})
        except MCPError as exc:
            self._obs.process.emit("blog_read_failed",
                                   blog_published_at=blog_published_at, reason=str(exc))
            return None

        flat = _flatten(result)
        # Not-found is a VALID result on this tool, never an error.
        if not flat or flat.get("found") is False:
            self._obs.process.emit("blog_read_empty", blog_published_at=blog_published_at)
            return None

        blog = BlogFacts(
            blog_published_at=_first(flat, "blog_published_at") or str(blog_published_at),
            blog_summary=_first(flat, "blog_summary"),
            blog_industry=_first(flat, "blog_industry"),
            ticket_id=_first(flat, "ticket_hs_id", "ticket_id"),
        )
        warnings = flat.get("warnings")
        if warnings:
            self._obs.process.emit("blog_read_warnings",
                                   blog_published_at=blog_published_at, warnings=warnings)
        self._obs.process.emit("blog_read_ok", blog_published_at=blog.blog_published_at,
                               ticket_id=blog.ticket_id, chars=len(blog.blog_summary))
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
            self._obs.process.emit("context_write_skipped", object_id=lead.object_id,
                                   reason="bad-data: the composed note was empty")
            return WriteResult(status="skipped", object_id=lead.object_id,
                               property_name=prop, tool=settings.mcp_tool_write,
                               error="bad-data: the composed note was empty")

        missing = lead.writable
        if missing:
            reason = (f"bad-data: lead is missing {missing}, which "
                      f"{settings.mcp_tool_write} requires to write")
            self._obs.process.emit("context_write_skipped", object_id=lead.object_id,
                                   reason=reason)
            return WriteResult(status="skipped", object_id=lead.object_id,
                               property_name=prop, tool=settings.mcp_tool_write,
                               error=reason)

        if settings.dry_run:
            self._obs.process.emit("context_write_dry_run", object_id=lead.object_id,
                                   property_name=prop, chars=len(text))
            return WriteResult(status="dry_run", object_id=lead.object_id,
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
            self._obs.process.emit("context_write_failed", object_id=lead.object_id,
                                   tool=settings.mcp_tool_write, reason=str(exc))
            return WriteResult(status="error", object_id=lead.object_id,
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
                self._obs.process.emit("context_write_rejected", object_id=lead.object_id,
                                       tool=settings.mcp_tool_write, reason=reason)
                return WriteResult(status="error", object_id=lead.object_id,
                                   property_name=prop, chars=len(text),
                                   tool=settings.mcp_tool_write, error=reason)

        self._obs.process.emit(
            "context_write_ok", object_id=lead.object_id, property_name=prop,
            chars=len(text),
            note="this write raises HubSpot trigger 2 (contact.propertyChange "
                 "lead_context) which the gateway routes to the Email agent")
        return WriteResult(status="written", object_id=lead.object_id,
                           property_name=prop, chars=len(text),
                           tool=settings.mcp_tool_write)
