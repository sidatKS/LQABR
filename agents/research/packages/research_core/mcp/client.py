"""The MCP client — how this agent reaches the HubSpot MCP container.

    initialize                 -> capture Mcp-Session-Id
    notifications/initialized
    tools/list                 -> DISCOVER the surface, bind our three names
    tools/call                 -> the work

Why discovery matters: the tool names live in configuration
(``LQABR_RESEARCH_MCP_TOOL_*``), and configuration can be wrong. Asking the
server what it actually has, at startup, turns "the write silently did nothing
in production" into "the container refused to start and said which tool it
could not find". That is what ``ensure_ready()`` is for.

Transport notes:
* MCP's streamable-HTTP binding may answer with ``application/json`` OR with an
  SSE stream carrying one ``data:`` frame. Both are handled.
* A dropped session (HTTP 404) is re-initialized once and the call retried.
* Retries: bounded, exponential backoff, on the configured statuses and on
  transport errors. Every attempt lands on the audit stream with endpoint,
  method, status and duration. Never a payload, never the token.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import requests

from ..obs import Observability, get_obs
from ..settings import Settings, get_settings

#: Fallback only — the live list comes from settings.mcp_retryable_statuses.
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)


class MCPError(RuntimeError):
    """The MCP could not be reached, or refused. Always names what failed."""


class MCPToolMissing(MCPError):
    """A configured tool is not on the server. Raised at startup, by name."""


def _parse_body(response: requests.Response) -> Optional[Dict[str, Any]]:
    """A JSON-RPC reply out of either transport shape. None for a notification."""
    text = (response.text or "").strip()
    if not text:
        return None
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "event-stream" in content_type or text.startswith(("event:", "data:")):
        message = None
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    candidate = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                    message = candidate
        return message
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def unwrap_result(result: Any) -> Any:
    """MCP wraps a tool's return in content blocks. Give back the value.

    ``{"content": [{"type": "text", "text": "{\\"status\\": \\"ok\\"}"}]}``
    becomes ``{"status": "ok"}``. A server that returns the value directly is
    passed straight through, so both shapes work.
    """
    if not isinstance(result, dict):
        return result
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    if not isinstance(content, list):
        return result
    texts = [block.get("text", "") for block in content
             if isinstance(block, dict) and block.get("type") == "text"]
    if not texts:
        return result
    joined = "\n".join(t for t in texts if t)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


class MCPClient:
    """One connection to the HubSpot MCP container. Build one per process."""

    def __init__(self, settings: Settings | None = None, *,
                 session: Optional[requests.Session] = None,
                 obs: Observability | None = None) -> None:
        self._settings = settings or get_settings()
        self._session = session or requests.Session()
        self._obs = obs or get_obs()
        self._mcp_session_id: str = ""
        self._initialized = False
        self._tools: List[str] = []
        self._rpc_id = 0

    @property
    def tools(self) -> List[str]:
        """What the server said it has. Empty until tools/list has run."""
        return list(self._tools)

    # ------------------------------------------------------------ wire
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Both, because the server chooses which transport to answer with.
            "Accept": "application/json, text/event-stream",
        }
        if self._mcp_session_id:
            headers["Mcp-Session-Id"] = self._mcp_session_id
            headers["MCP-Protocol-Version"] = self._settings.mcp_protocol_version
        if self._settings.mcp_auth_token:
            headers["Authorization"] = f"Bearer {self._settings.mcp_auth_token}"
        return headers

    def _post(self, payload: Dict[str, Any], *, label: str) -> requests.Response:
        url = self._settings.mcp_base_url
        started = time.monotonic()
        try:
            response = self._session.request(
                "POST", url, headers=self._headers(), json=payload,
                timeout=self._settings.mcp_timeout_seconds,
            )
        except (requests.RequestException, OSError) as exc:
            self._obs.hop(service="mcp", endpoint=url, error=str(exc),
                          duration_ms=round((time.monotonic() - started) * 1000, 1))
            raise MCPError(f"MCP {label} failed: {exc}") from exc
        self._obs.hop(service="mcp", endpoint=url, status=response.status_code,
                      duration_ms=round((time.monotonic() - started) * 1000, 1))
        session_id = (response.headers.get("Mcp-Session-Id")
                      or response.headers.get("mcp-session-id"))
        if session_id:
            self._mcp_session_id = session_id
        return response

    def _sleep(self, attempt: int) -> None:
        delay = self._settings.mcp_backoff_base_seconds * (2 ** (attempt - 1))
        time.sleep(min(delay, self._settings.mcp_backoff_cap_seconds))

    # ------------------------------------------------------------ handshake
    def initialize(self, force: bool = False) -> None:
        if self._initialized and not force:
            return
        self._mcp_session_id = ""
        attempts = max(1, self._settings.max_retries)
        response = None
        for attempt in range(1, attempts + 1):
            self._rpc_id += 1
            response = self._post({
                "jsonrpc": "2.0", "id": self._rpc_id, "method": "initialize",
                "params": {
                    "protocolVersion": self._settings.mcp_protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "lqabr-research-agent", "version": "0.1"},
                },
            }, label="initialize")
            if (response.status_code in tuple(self._settings.mcp_retryable_statuses)
                    and attempt < attempts):
                self._obs.process.emit("mcp_initialize_retry", attempt=attempt,
                                       status=response.status_code)
                self._sleep(attempt)
                continue
            break
        assert response is not None
        if response.status_code >= 400:
            raise MCPError(f"MCP initialize failed: HTTP {response.status_code} from "
                           f"{self._settings.mcp_base_url}")
        body = _parse_body(response)
        if body and body.get("error"):
            raise MCPError(f"MCP initialize rejected: {body['error']}")

        notified = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                              label="notifications/initialized")
        if notified.status_code >= 400:
            raise MCPError(f"MCP initialized-notification failed: HTTP {notified.status_code}")
        self._initialized = True
        self._obs.process.emit("mcp_initialized", url=self._settings.mcp_base_url,
                               protocol=self._settings.mcp_protocol_version,
                               session="set" if self._mcp_session_id else "none")

    def list_tools(self) -> List[str]:
        """Ask the server what it has. The names we bind come from here."""
        self.initialize()
        self._rpc_id += 1
        response = self._post({"jsonrpc": "2.0", "id": self._rpc_id,
                               "method": "tools/list", "params": {}}, label="tools/list")
        if response.status_code >= 400:
            raise MCPError(f"MCP tools/list failed: HTTP {response.status_code}")
        body = _parse_body(response) or {}
        if body.get("error"):
            raise MCPError(f"MCP tools/list error: {body['error']}")
        tools = ((body.get("result") or {}).get("tools")) or []
        self._tools = [t.get("name", "") for t in tools
                       if isinstance(t, dict) and t.get("name")]
        self._obs.process.emit("mcp_tools_discovered", count=len(self._tools),
                               tools=sorted(self._tools))
        return list(self._tools)

    def ensure_ready(self, required: Optional[List[str]] = None) -> List[str]:
        """Connect, discover, and fail LOUDLY if a configured tool is absent —
        at startup, rather than as a write that quietly did nothing."""
        tools = self.list_tools()
        missing = [name for name in (required or []) if name and name not in tools]
        if missing and self._settings.mcp_assert_tools:
            raise MCPToolMissing(
                f"the MCP at {self._settings.mcp_base_url} does not expose {missing} — "
                f"it offers {sorted(tools)}. Point LQABR_RESEARCH_MCP_TOOL_READ_LEAD / "
                "_READ_BLOG / _WRITE at the real names (config change, no code edit), "
                "or set LQABR_RESEARCH_MCP_ASSERT_TOOLS=0 to start anyway.")
        if missing:
            self._obs.process.emit("mcp_tools_missing_ignored", missing=missing,
                                   reason="LQABR_RESEARCH_MCP_ASSERT_TOOLS=0")
        return tools

    # ------------------------------------------------------------ call
    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """tools/call, with the house retry contract and one session recovery."""
        self.initialize()
        attempts = max(1, self._settings.max_retries)
        last_error = ""

        for attempt in range(1, attempts + 1):
            self._rpc_id += 1
            response = self._post({
                "jsonrpc": "2.0", "id": self._rpc_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }, label=f"tools/call {name}")

            if response.status_code == 404 and attempt < attempts:
                # The server forgot our session (scaled to zero between calls).
                self._obs.process.emit("mcp_session_lost", tool=name, attempt=attempt)
                self.initialize(force=True)
                continue
            if (response.status_code in tuple(self._settings.mcp_retryable_statuses)
                    and attempt < attempts):
                last_error = f"HTTP {response.status_code}"
                self._sleep(attempt)
                continue
            if response.status_code >= 400:
                raise MCPError(f"MCP tools/call {name} failed: HTTP {response.status_code}")

            body = _parse_body(response)
            if body is None:
                raise MCPError(f"MCP tools/call {name} returned no parseable body")
            if body.get("error"):
                raise MCPError(f"MCP tools/call {name} error: {body['error']}")
            result = body.get("result")
            if isinstance(result, dict) and result.get("isError"):
                raise MCPError(f"MCP tool {name} reported an error: {unwrap_result(result)}")
            return unwrap_result(result)

        raise MCPError(f"MCP tools/call {name} failed after {attempts} attempts: {last_error}")
