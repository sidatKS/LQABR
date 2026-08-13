"""A2A (Agent-to-Agent protocol) dispatcher.

The orchestrator talks to the stage agents over Google's A2A protocol —
JSON-RPC 2.0 `message/send` against each agent's endpoint. Agent endpoints
are config-driven (env vars set by infra/gcp/05_deploy_agents.sh):

    LQABR_LEAD_PROFILE_AGENT_URL
    LQABR_EMAIL_AGENT_URL
    LQABR_TEXT_VOICE_AGENT_URL
    LQABR_SCHEDULING_AGENT_URL

Swapping an agent implementation is a URL change, never a code edit.
Typed and mockable — inject a requests.Session in tests.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

import requests

AGENT_URL_ENV = {
    "lead_profile": "LQABR_LEAD_PROFILE_AGENT_URL",
    "email": "LQABR_EMAIL_AGENT_URL",
    "text_voice": "LQABR_TEXT_VOICE_AGENT_URL",
    "scheduling": "LQABR_SCHEDULING_AGENT_URL",
}


class A2AError(RuntimeError):
    pass


def agent_url(agent_key: str) -> str:
    env = AGENT_URL_ENV.get(agent_key)
    if env is None:
        raise A2AError(f"unknown agent '{agent_key}' — one of {sorted(AGENT_URL_ENV)}")
    url = os.environ.get(env, "")
    if not url:
        raise A2AError(f"{env} is not configured")
    return url


class A2ADispatcher:
    def __init__(self, session: Optional[requests.Session] = None, timeout: int = 120) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout

    def send_message(self, agent_key: str, text: str,
                     metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send one A2A `message/send` task to a stage agent; returns the
        JSON-RPC result. Raises A2AError on transport or protocol failure —
        the orchestrator flags the lead rather than losing the work item."""
        url = agent_url(agent_key)
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": text}],
                    "messageId": str(uuid.uuid4()),
                },
                "metadata": metadata or {},
            },
        }
        try:
            resp = self._session.post(url, json=body, timeout=self._timeout)
        except requests.RequestException as exc:
            raise A2AError(f"A2A send to {agent_key} failed: {exc}") from exc
        if resp.status_code != 200:
            raise A2AError(f"A2A send to {agent_key} failed: HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        if "error" in payload:
            raise A2AError(f"A2A {agent_key} returned error: {payload['error']}")
        return payload.get("result", {})
