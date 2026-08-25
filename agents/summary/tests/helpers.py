"""Test doubles. Everything here is offline by construction."""

from __future__ import annotations

import json as jsonlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    url: str = "https://example.com/post"
    _json: Any = None

    def json(self) -> Any:
        if self._json is not None:
            return self._json
        return jsonlib.loads(self.text)


@dataclass
class FakeSession:
    """A requests.Session stand-in that records what it was asked to do.

    `responses` is consumed in order, so a test can assert on retry
    behaviour by queueing a 503 followed by a 200.
    """

    responses: List[Any] = field(default_factory=list)
    calls: List[Dict[str, Any]] = field(default_factory=list)

    def request(self, method: str, url: str, headers: Optional[Dict[str, str]] = None,
                json: Any = None, timeout: Optional[float] = None) -> FakeResponse:
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "json": json, "timeout": timeout})
        if not self.responses:
            raise AssertionError(f"FakeSession ran out of responses at call {len(self.calls)}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def html_page(title: str = "A Post", body: str = "First line.\nSecond line.") -> str:
    return f"""<!doctype html><html><head><title>{title} | Example Blog</title></head>
    <body>
      <nav>Home About Contact</nav>
      <header>site header</header>
      <article><h1>{title}</h1><p>{body}</p></article>
      <aside>Related posts</aside>
      <footer>(c) Example</footer>
      <script>console.log('tracking')</script>
    </body></html>"""


# ---------------------------------------------------------------- MCP


@dataclass
class FakeMCPSession:
    """A HubSpot MCP container, faked at the JSON-RPC layer.

    Routes on the RPC method, so a test writes what the SERVER has rather
    than a queue of opaque responses. `tools` is the surface it advertises —
    set it to a different list and the client's discovery/binding behaviour
    is exercised exactly as it would be against a renamed real server.
    """

    tools: List[str] = field(default_factory=lambda: [
        "get_lead_profile_details", "list_trigger_leads", "post_patch_crm"])
    tool_results: Dict[str, Any] = field(default_factory=dict)
    calls: List[Dict[str, Any]] = field(default_factory=list)
    #: status code -> returned once, then normal service resumes. Lets a test
    #: queue a 503 or a 404 (a dropped session) without scripting everything.
    status_queue: List[int] = field(default_factory=list)
    session_id: str = "mcp-session-1"
    initialize_count: int = 0
    fail_with: Optional[Exception] = None

    def request(self, method: str, url: str, headers: Optional[Dict[str, str]] = None,
                json: Any = None, timeout: Optional[float] = None) -> FakeResponse:
        if self.fail_with is not None:
            raise self.fail_with
        payload = json or {}
        rpc_method = payload.get("method", "")
        self.calls.append({"method": rpc_method, "params": payload.get("params"),
                           "headers": dict(headers or {})})

        if self.status_queue:
            return FakeResponse(status_code=self.status_queue.pop(0), text="{}")

        if rpc_method == "initialize":
            self.initialize_count += 1
            return self._ok({"protocolVersion": "2025-06-18", "capabilities": {}},
                            with_session=True)
        if rpc_method == "notifications/initialized":
            return FakeResponse(status_code=202, text="")
        if rpc_method == "tools/list":
            return self._ok({"tools": [{"name": name} for name in self.tools]})
        if rpc_method == "tools/call":
            params = payload.get("params") or {}
            name = params.get("name", "")
            if name not in self.tools:
                return self._ok({"isError": True,
                                 "content": [{"type": "text", "text": f"unknown tool {name}"}]})
            value = self.tool_results.get(name, {"status": "written"})
            if isinstance(value, Exception):
                raise value
            return self._ok({"content": [{"type": "text", "text": jsonlib.dumps(value)}]})
        return FakeResponse(status_code=400, text="{}")

    def _ok(self, result: Any, with_session: bool = False) -> FakeResponse:
        headers = {"Content-Type": "application/json"}
        if with_session:
            headers["Mcp-Session-Id"] = self.session_id
        return FakeResponse(
            status_code=200,
            text=jsonlib.dumps({"jsonrpc": "2.0", "id": 1, "result": result}),
            headers=headers,
        )

    @property
    def tool_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "tools/call"]
