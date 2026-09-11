"""The agent's tools — plain Python functions, no ADK types.

ADK takes ordinary callables, so keeping them ADK-free means the whole tool
surface is unit-testable without a runner, a session or a model, and the
same functions back the deterministic pipeline. The docstrings ARE the tool
descriptions the model reads; they are written for that audience.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from summary_core import sources
from summary_core.mcp.hubspot import HubSpotMCP
from summary_core.summary_logging import SummaryLogging, get_obs
from summary_core.settings import Settings, get_settings
from summary_core.types import SourceError, SourceSpec, SummaryResult

#: Set once at start-up by agent.py / service_app.py. The ADK tool signatures
#: cannot carry these, so they are process state — one agent per process.
_SETTINGS: Optional[Settings] = None
_HUBSPOT: Optional[HubSpotMCP] = None


def configure(settings: Settings | None = None, hubspot: HubSpotMCP | None = None) -> None:
    global _SETTINGS, _HUBSPOT
    _SETTINGS = settings or get_settings()
    _HUBSPOT = hubspot or HubSpotMCP(settings=_SETTINGS)


def _settings() -> Settings:
    return _SETTINGS or get_settings()


def _hubspot() -> HubSpotMCP:
    global _HUBSPOT
    if _HUBSPOT is None:
        _HUBSPOT = HubSpotMCP(settings=_settings())
    return _HUBSPOT


# ---------------------------------------------------------------- tools
def fetch_document(kind: str, reference: str = "", select: str = "",
                   method: str = "GET", payload_json: str = "") -> Dict[str, Any]:
    """Fetch whatever should be summarised and return it as one document.

    Use this before summarising anything. It handles every input the agent
    accepts, so you never need to fetch a URL yourself.

    Args:
        kind: one of "url", "api", "json", "text".
        reference: for "url" the page address; for "api" the endpoint; for
            "text" the text itself. Ignored for "json".
        select: optional path into a JSON response, e.g. "$.data.article.body".
        method: HTTP method for "api". Defaults to GET.
        payload_json: for "json", the payload as a JSON string; for "api", an
            optional JSON request body.

    Returns:
        The document, with its text, title and metadata — or an "error" key
        naming why the source could not be read.
    """
    obs = get_obs()
    try:
        payload = json.loads(payload_json) if payload_json.strip() else None
    except json.JSONDecodeError as exc:
        return {"error": f"payload_json is not valid JSON: {exc.msg}"}

    try:
        spec = SourceSpec(
            kind=(kind or "").strip().lower(),
            url=reference if kind == "url" else None,
            endpoint=reference if kind == "api" else None,
            text=reference if kind == "text" else None,
            method=method or "GET",
            payload=payload if kind == "json" else None,
            body=payload if kind == "api" else None,
            select=select or None,
        )
        document = sources.fetch(spec, _settings(), obs=obs)
    except SourceError as exc:
        obs.process.emit("tool_fetch_failed", kind=kind, reason=str(exc))
        return {"error": str(exc)}
    return document.to_dict()


def write_summary_to_hubspot(object_id: str, summary_json: str,
                             industry: str = "") -> Dict[str, Any]:
    """Write a finished summary to HubSpot through the HubSpot MCP.

    Call this only after you have a summary. The summary lands on the
    configured summary property of the configured object, which is what
    triggers the downstream campaign.

    Args:
        object_id: the HubSpot record to write to (for example the Ticket id).
        summary_json: the summary object as a JSON string, in the same shape
            you were asked to produce.
        industry: optional industry to write alongside the summary.

    Returns:
        {"status": "written"} on success, or a status of "error"/"skipped"
        with the reason. A failed write never reports success.
    """
    try:
        data = json.loads(summary_json) if summary_json.strip() else {}
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": f"summary_json is not valid JSON: {exc.msg}"}
    if not isinstance(data, dict):
        return {"status": "error", "error": "summary_json must be a JSON object"}

    result = SummaryResult(
        summary=str(data.get("summary", "")),
        title=str(data.get("title", "") or ""),
        topic=str(data.get("topic", "") or ""),
        key_points=list(data.get("key_points") or []),
        concepts=list(data.get("concepts") or []),
        technologies=list(data.get("technologies") or []),
        takeaways=list(data.get("takeaways") or []),
        industry=str(data.get("industry", "") or ""),
    )
    if not result.summary.strip():
        return {"status": "skipped",
                "error": "there is no summary text to write — nothing was sent"}
    return _hubspot().write_summary(object_id, result, industry=industry).to_dict()


def get_lead_profile(object_id: str) -> Dict[str, Any]:
    """Read a lead's profile from HubSpot through the MCP, to frame a summary
    for a specific reader.

    Args:
        object_id: the HubSpot record id.

    Returns:
        The profile, or an empty object when the record has none. An empty
        result is not an error: summarise the document without it.
    """
    return _hubspot().get_lead_profile(object_id)


#: What agent.py hands to ADK. Order matters only for readability.
AGENT_TOOLS = [fetch_document, get_lead_profile, write_summary_to_hubspot]
