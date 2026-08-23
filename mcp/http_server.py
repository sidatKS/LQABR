"""HubSpot MCP — streamable-HTTP front door for the local MVP.

The tool logic lives in mcp/hubspot/server.py (MCPSession); this module is
transport only. It speaks the MCP streamable-HTTP JSON-RPC binding that the
Summary and Text/Voice agents' clients already implement:

    initialize                 -> returns a result, sets Mcp-Session-Id header
    notifications/initialized  -> acknowledged
    tools/list                 -> the five tool names below
    tools/call                 -> dispatch to MCPSession, wrap the return

Run it:
    uvicorn mcp.http_server:app --host 0.0.0.0 --port 8080     # in-container
    # container maps host 8081 -> container 8080; agents dial :8081/mcp

Two vocabularies, one surface (MVP 2026-08-21). The Summary agent calls
`get_lead_profile_details` / `post_patch_crm` / `list_trigger_leads`; the
Text/Voice agent calls `get_lead_profile` / `upsert_lead_profile` with the
SAME argument shapes. Both names are exposed and aliased onto the same
MCPSession methods, so neither agent needs a code change.

Auth: the HubSpot token is resolved by mcp.hubspot.auth via lqabr_core.secrets.
Set LQABR_SECRETS_SOURCE=env and LQABR_HUBSPOT_ACCESS_TOKEN=<token> for local
runs; nothing is hard-coded here.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from mcp.hubspot.server import build_session

MCP_PATH = os.environ.get("LQABR_MCP_PATH", "/mcp")
PROTOCOL_VERSION = os.environ.get("LQABR_MCP_PROTOCOL_VERSION", "2025-06-18")
SERVER_NAME = "lqabr-hubspot-mcp"
SERVER_VERSION = "0.1.0-mvp"

app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION)


# --------------------------------------------------------------------- tools
def _tool_get_profile(session, args: Dict[str, Any]) -> Any:
    return session.get_lead_profile_details(str(args.get("object_id", "")))


def _tool_list_leads(session, args: Dict[str, Any]) -> Any:
    limit = int(args.get("limit", 25) or 25)
    return session.list_trigger_leads(str(args.get("object_id", "")), limit=limit)


def _tool_patch(session, args: Dict[str, Any]) -> Any:
    return session.post_patch_crm(
        str(args.get("object_id", "")),
        dict(args.get("properties") or {}),
        object_type=str(args.get("object_type", "contact") or "contact"),
    )


#: name -> (handler, description, input-property names). The Text/Voice
#: aliases point at the same handlers as the Summary names.
TOOLS: Dict[str, Dict[str, Any]] = {
    "get_lead_profile_details": {
        "fn": _tool_get_profile,
        "desc": "Read a lead's profile from HubSpot by contact object_id.",
        "props": {"object_id": "string"},
    },
    "get_lead_profile": {  # Text/Voice alias
        "fn": _tool_get_profile,
        "desc": "Alias of get_lead_profile_details (Text/Voice vocabulary).",
        "props": {"object_id": "string"},
    },
    "list_trigger_leads": {
        "fn": _tool_list_leads,
        "desc": "The lead profiles HubSpot chunked under one trigger object_id.",
        "props": {"object_id": "string", "limit": "integer"},
    },
    "post_patch_crm": {
        "fn": _tool_patch,
        "desc": "Write properties onto a HubSpot object (object_type: contact|ticket).",
        "props": {"object_id": "string", "properties": "object", "object_type": "string"},
    },
    "upsert_lead_profile": {  # Text/Voice alias
        "fn": _tool_patch,
        "desc": "Alias of post_patch_crm (Text/Voice vocabulary); writes contact properties.",
        "props": {"object_id": "string", "properties": "object"},
    },
}


def _tools_list_payload() -> List[Dict[str, Any]]:
    out = []
    for name, spec in TOOLS.items():
        out.append({
            "name": name,
            "description": spec["desc"],
            "inputSchema": {
                "type": "object",
                "properties": {k: {"type": v} for k, v in spec["props"].items()},
                "required": ["object_id"],
            },
        })
    return out


def _rpc_result(rpc_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _rpc_error(rpc_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _wrap_tool_return(value: Any) -> Dict[str, Any]:
    """MCP tools/call result envelope. structuredContent is what both agent
    clients read first; the text block mirrors it for any stricter client."""
    return {
        "structuredContent": value,
        "content": [{"type": "text", "text": json.dumps(value)}],
        "isError": False,
    }


# --------------------------------------------------------------------- routes
@app.get("/health")
@app.get("/healthz")
def health() -> Dict[str, Any]:
    return {
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "protocol": PROTOCOL_VERSION,
        "tools": sorted(TOOLS.keys()),
        "secrets_source": os.environ.get("LQABR_SECRETS_SOURCE", "auto"),
        "token_present": bool(os.environ.get("LQABR_HUBSPOT_ACCESS_TOKEN")),
    }


@app.post(MCP_PATH)
async def mcp_endpoint(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(_rpc_error(None, -32700, "parse error"), status_code=400)

    method = body.get("method")
    rpc_id = body.get("id")

    # Notifications carry no id and expect no body.
    if method == "notifications/initialized":
        return Response(status_code=200)

    if method == "initialize":
        payload = _rpc_result(rpc_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        resp = JSONResponse(payload)
        resp.headers["Mcp-Session-Id"] = uuid.uuid4().hex
        return resp

    if method == "tools/list":
        return JSONResponse(_rpc_result(rpc_id, {"tools": _tools_list_payload()}))

    if method == "tools/call":
        params = body.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            return JSONResponse(_rpc_error(rpc_id, -32601, f"unknown tool: {name}"))
        try:
            session = build_session()
            value = spec["fn"](session, args)
        except Exception as exc:  # noqa: BLE001 - surface as an MCP tool error
            err = {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }
            return JSONResponse(_rpc_result(rpc_id, err))
        return JSONResponse(_rpc_result(rpc_id, _wrap_tool_return(value)))

    # Unknown method: a well-formed JSON-RPC method-not-found.
    return JSONResponse(_rpc_error(rpc_id, -32601, f"method not found: {method}"))
