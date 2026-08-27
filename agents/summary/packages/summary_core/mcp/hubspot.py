"""What this agent asks the HubSpot MCP to do — one method per operation.

Everything HubSpot-shaped is confined to this file. If the container's tool
names, argument names or property names ever change, this is the only module
that moves, and most of the time the change is an environment variable
rather than an edit here.

The write is the point of the agent: a summary landing on the configured
property is what the gateway's blog-summary route is waiting for. A write
that fails is reported as a failure — never smoothed into a success.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..obs import Observability, get_obs
from ..settings import Settings, get_settings
from ..types import SummaryResult, WriteResult
from .client import MCPClient, MCPError

import re as _re


def _iso_published_at(value: str) -> str:
    """Normalise blog_published_at to full ISO 8601 for the FastMCP tool's
    datetime-keyed upsert. A bare 'YYYY-MM-DD' becomes 'YYYY-MM-DDT00:00:00.000Z';
    a value that already carries a time/zone is passed through unchanged. A bare
    date is what silently no-ops the upsert — every real HubSpot row is a full
    datetime (see the working manual curl)."""
    v = str(value or "").strip()
    if not v or "T" in v:
        return v
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v + "T00:00:00.000Z"
    return v


def _normalise_industry(value: str, allowed: tuple = ()) -> str:
    """Coerce a free-text industry to the portal's dropdown spelling.

    The model returns prose ("Healthcare", "financial services"); HubSpot stores
    an enum ("HEALTHCARE", "FINANCIAL_SERVICES") and rejects anything else. The
    MCP's own error says why that strictness matters: "a near-miss selects zero
    leads and raises no error" — so a wrong-but-accepted value is worse than a
    rejection.

    Normalisation is therefore deliberately conservative: case and separators
    only. A value that still does not match a configured option is returned as
    normalised and left for the MCP to reject, rather than fuzzy-matched to the
    nearest option — guessing which industry a lead belongs to is not this
    function's job.
    """
    text = _re.sub(r"[\s\-/]+", "_", str(value or "").strip()).upper()
    text = _re.sub(r"_+", "_", text).strip("_")
    if not text:
        return ""
    for option in allowed or ():
        if option.strip().upper() == text:
            return option          # return the portal's exact spelling
    return text


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
        """Startup check — the tools we are configured to use must exist."""
        return self._client.ensure_ready([
            self._settings.mcp_tool_write,
            self._settings.mcp_tool_read,
        ])

    # ------------------------------------------------------------- write
    def write_summary(self, object_id: str, summary: SummaryResult, *,
                      industry: str = "", object_type: str = "",
                      subject: str = "", blog_published_at: str = "",
                      extra_properties: Optional[Dict[str, Any]] = None) -> WriteResult:
        """Land the summary on the CRM through the MCP."""
        settings = self._settings
        object_type = object_type or settings.hubspot_object_type

        # --- blog_summary style: the FastMCP central server's upsert_blog_summary,
        # keyed on blog_published_at. Flat required args, no object_id / properties bag.
        if settings.mcp_write_style == "blog_summary":
            return self._write_blog_summary(summary, industry=industry, object_type=object_type,
                                            subject=subject, blog_published_at=blog_published_at)

        if not str(object_id or "").strip():
            return WriteResult(status="skipped", object_type=object_type,
                               error="no object_id was supplied, so there is nothing to write to")

        properties: Dict[str, Any] = {
            settings.hubspot_summary_property: summary.as_hubspot_text(),
        }
        resolved_industry = _normalise_industry(industry or summary.industry,
                                                settings.hubspot_industry_options)
        if resolved_industry and settings.hubspot_industry_property:
            properties[settings.hubspot_industry_property] = resolved_industry
        properties.update(extra_properties or {})

        if settings.dry_run:
            self._obs.process.emit(
                "hubspot_write_dry_run", object_id=object_id, object_type=object_type,
                properties=sorted(properties), chars=len(properties[settings.hubspot_summary_property]),
            )
            return WriteResult(status="dry_run", object_id=str(object_id),
                               object_type=object_type, properties=sorted(properties),
                               tool=settings.mcp_tool_write)

        arguments = {
            settings.mcp_arg_object_id: str(object_id),
            settings.mcp_arg_properties: properties,
        }
        try:
            result = self._client.call_tool(settings.mcp_tool_write, arguments)
        except MCPError as exc:
            self._obs.process.emit("hubspot_write_failed", object_id=object_id,
                                   tool=settings.mcp_tool_write, reason=str(exc))
            return WriteResult(status="error", object_id=str(object_id),
                               object_type=object_type, properties=sorted(properties),
                               tool=settings.mcp_tool_write, error=str(exc))

        # The MCP reports a rejected write as {"error": "..."} rather than by
        # raising. Honour that: a validation failure is not a success.
        if isinstance(result, dict) and result.get("error"):
            self._obs.process.emit("hubspot_write_rejected", object_id=object_id,
                                   tool=settings.mcp_tool_write, reason=str(result["error"]))
            return WriteResult(status="error", object_id=str(object_id),
                               object_type=object_type, properties=sorted(properties),
                               tool=settings.mcp_tool_write, error=str(result["error"]))

        self._obs.process.emit("hubspot_write_ok", object_id=object_id,
                               object_type=object_type, properties=sorted(properties),
                               tool=settings.mcp_tool_write)
        return WriteResult(status="written", object_id=str(object_id),
                           object_type=object_type, properties=sorted(properties),
                           tool=settings.mcp_tool_write)

    # ------------------------------------------------ write (blog_summary style)
    def _write_blog_summary(self, summary: SummaryResult, *, industry: str,
                            object_type: str, subject: str,
                            blog_published_at: str) -> WriteResult:
        """upsert_blog_summary{subject, blog_summary, blog_published_at, blog_industry}.
        All four are required by the tool; a missing one is reported, never sent blank."""
        settings = self._settings
        tool = settings.mcp_tool_write
        args = {
            "subject": (subject or summary.title or "").strip(),
            "blog_summary": summary.as_hubspot_text(),
            "blog_published_at": _iso_published_at(blog_published_at),
            "blog_industry": _normalise_industry(industry or summary.industry,
                                                 settings.hubspot_industry_options),
        }
        missing = [k for k in ("subject", "blog_summary", "blog_published_at", "blog_industry")
                   if not args[k]]
        if missing:
            self._obs.process.emit("hubspot_write_skipped", tool=tool, missing=missing)
            return WriteResult(status="skipped", object_type=object_type, tool=tool,
                               properties=sorted(args.keys()),
                               error=f"blog_summary write needs {missing} — nothing sent")

        if settings.dry_run:
            preview = dict(args); preview["blog_summary"] = f"<{len(args['blog_summary'])} chars>"
            self._obs.process.emit("hubspot_write_dry_run", tool=tool, args=preview)
            return WriteResult(status="dry_run", object_type=object_type, tool=tool,
                               properties=sorted(args.keys()))

        try:
            result = self._client.call_tool(tool, args)
            import json as _json
            self._obs.process.emit(
                "hubspot_write_raw_result", tool=tool,
                result_type=type(result).__name__,
                result_keys=(sorted(result.keys()) if isinstance(result, dict) else None),
                result_preview=_json.dumps(result)[:600] if result is not None else "None",
                sent_published_at=args.get("blog_published_at"))
        except MCPError as exc:
            self._obs.process.emit("hubspot_write_failed", tool=tool, reason=str(exc))
            return WriteResult(status="error", object_type=object_type, tool=tool,
                               properties=sorted(args.keys()), error=str(exc))
        # A rejected write is reported by the tool as a body, not by raising:
        #   {"error": ...}                              -> classic rejection
        #   {"status": "halted"/"failed", "reasons":[]} -> systemic failure
        #      (e.g. the MCP could not read the HubSpot token from Secret Manager).
        # Treat ALL of these as failures — never report a non-write as written.
        if isinstance(result, dict):
            status_val = str(result.get("status", "")).lower()
            if result.get("error") or result.get("failure_kind") or status_val in ("halted", "failed", "error"):
                reason = (str(result.get("error") or "")
                          or "; ".join(str(r) for r in (result.get("reasons") or []))
                          or status_val or "write rejected")
                self._obs.process.emit("hubspot_write_rejected", tool=tool, reason=reason)
                return WriteResult(status="error", object_type=object_type, tool=tool,
                                   properties=sorted(args.keys()), error=reason)

        ticket_id = ""
        if isinstance(result, dict):
            ticket_id = str(result.get("ticket_hs_id") or result.get("ticket_id")
                            or result.get("id") or result.get("object_id") or "")
        self._obs.process.emit("hubspot_write_ok", tool=tool, ticket_id=ticket_id,
                               properties=sorted(args.keys()))
        return WriteResult(status="written", object_id=ticket_id, object_type=object_type,
                           tool=tool, properties=sorted(args.keys()))

    # -------------------------------------------------------------- read
    def get_lead_profile(self, object_id: str) -> Dict[str, Any]:
        """The read path, for a summary that should be framed by who it is for.

        Returns `{}` when the MCP has nothing — the caller summarises without
        the profile rather than failing. A profile is a nice-to-have here;
        the document is the job.
        """
        try:
            result = self._client.call_tool(
                self._settings.mcp_tool_read,
                {self._settings.mcp_arg_object_id: str(object_id)},
            )
        except MCPError as exc:
            self._obs.process.emit("hubspot_read_failed", object_id=object_id, reason=str(exc))
            return {}
        if isinstance(result, dict) and result.get("error"):
            self._obs.process.emit("hubspot_read_rejected", object_id=object_id,
                                   reason=str(result["error"]))
            return {}
        return result if isinstance(result, dict) else {}

    def list_trigger_leads(self, object_id: str, limit: int = 25) -> List[Dict[str, Any]]:
        """The leads HubSpot chunked under one object id, if the server has it."""
        try:
            result = self._client.call_tool(
                self._settings.mcp_tool_list_leads,
                {self._settings.mcp_arg_object_id: str(object_id), "limit": limit},
            )
        except MCPError as exc:
            self._obs.process.emit("hubspot_list_failed", object_id=object_id, reason=str(exc))
            return []
        return result if isinstance(result, list) else []
