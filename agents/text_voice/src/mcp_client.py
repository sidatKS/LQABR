"""STEP 5 — the central MCP server, over the real MCP protocol."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests

from text_voice_core import observability as obs, settings
from text_voice_core.gcp_id_token import auth_header
from text_voice_core.types import CRMError, EventType, VoiceLead
from text_voice_core.probability import SCHEDULING_THRESHOLD, apply_event

try:
    from .tools import retrying_call
except ImportError:  # pragma: no cover - uvicorn/pytest put src/ on sys.path
    from tools import retrying_call  # type: ignore

MCP_BASE_URL = settings.MCP_BASE_URL
MCP_TIMEOUT_SECONDS = settings.MCP_TIMEOUT_SECONDS

_PROTOCOL_VERSION = "2025-06-18"

_VOICE_STATUS_VALUES = ("INITIATED", "COMPLETED", "FAILED",
                        "VOICEMAIL_LEFT", "CALL_PLACED")

_VOICE_STATUS_FOR_OUTCOME = {
    "not_answered": "FAILED",
    "voicemail": "VOICEMAIL_LEFT",
    "answered_not_engaged": "COMPLETED",
    "answered_and_engaged": "COMPLETED",
}
_EVENTS_FOR_OUTCOME = {
    "not_answered": (EventType.CALL_NOT_ANSWERED,),
    "voicemail": (EventType.VOICEMAIL_LEFT,),
    "answered_not_engaged": (EventType.CALL_ANSWERED,),
    "answered_and_engaged": (EventType.CALL_ANSWERED, EventType.CALL_ENGAGED),
}

_KEY_TO_FIELD = {
    "employee_id": "employee_id",
    "company_id": "company_id",
    "job_title": "job_title",
    "industry": "industry",
    "frequency_of_purchase": "frequency_of_purchase",
    "decision_maker_flag": "decision_maker",
    "decision_maker": "decision_maker",
    "email": "email",
    "phone": "phone_number",
    "phone_number": "phone_number",
    "contact_name": "full_name",
    "full_name": "full_name",
    "company_name": "company_name",
    "annual_revenue_m": "annual_revenue",
    "annual_revenue": "annual_revenue",
    "contact_hs_id": "object_id",
    "contact_id": "object_id",
    "company_hs_id": "hubspot_company_id",
    # Un-prefixed status properties (decided 2026-08-25; the old lqabr_*
    # prefixed names are retired).
    "voice_status": "voice_status",
    "email_status": "email_status",
    "probability": "probability",
    "opted_out": "opted_out",
}


def _flatten(data: Dict[str, Any]) -> Dict[str, Any]:
    """The reply as one flat dict, profile keys merged over the top level."""
    merged = dict(data)
    nested = merged.pop("profile", None)
    if isinstance(nested, dict):
        merged.update({k: v for k, v in nested.items() if v is not None})
    return merged


def _to_voice_lead(data: Dict[str, Any]) -> VoiceLead:
    """The server's lead dict as a VoiceLead — the single place wire keys are mapped."""
    flat = _flatten(data)
    fields: Dict[str, Any] = {}
    for key, field in _KEY_TO_FIELD.items():
        if flat.get(key) not in (None, ""):
            fields[field] = flat[key]
    try:
        fields["probability"] = int(float(fields.get("probability") or 0))
    except (TypeError, ValueError):
        fields["probability"] = 0
    opted = fields.get("opted_out")
    if not isinstance(opted, bool):
        fields["opted_out"] = str(opted or "").strip().lower() in ("true", "yes", "1")
    return VoiceLead(**fields)


def _is_not_found(result: Any) -> bool:
    """The shapes that mean "no such lead" rather than "the CRM broke"."""
    if result is None or result == {}:
        return True
    if isinstance(result, dict):
        if result.get("found") is False:
            return True
        if str(result.get("error") or "").startswith("not-found"):
            return True
    return False


class StepFiveMCPClient:
    """The Step 5 tool surface over MCP streamable HTTP (JSON-RPC 2.0)."""

    def __init__(self, base_url: Optional[str] = None,
                 session: Optional[requests.Session] = None,
                 max_retries: int = 3, backoff_seconds: float = 1.0) -> None:
        self._base_url = base_url or MCP_BASE_URL
        self._http = session or requests.Session()
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._session_id: Optional[str] = None
        self._lock = threading.Lock()
        self._rpc_id = 0

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # Google-signed ID token when the MCP is a private Cloud Run
            # service; nothing for loopback, so local dev is unchanged.
            **auth_header(self._base_url),
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = _PROTOCOL_VERSION
        return headers

    @staticmethod
    def _parse_body(resp: requests.Response) -> Optional[Dict[str, Any]]:
        """One JSON-RPC message out of a JSON or SSE response body."""
        content_type = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in content_type:
            message = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if not payload:
                        continue
                    try:
                        candidate = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict) and (
                            "result" in candidate or "error" in candidate):
                        message = candidate
            return message
        if resp.text:
            body = resp.json()
            return body if isinstance(body, dict) else None
        return None

    def _post(self, message: Dict[str, Any]) -> requests.Response:
        return self._http.post(self._base_url, json=message,
                               headers=self._headers(),
                               timeout=MCP_TIMEOUT_SECONDS)

    def _initialize(self) -> None:
        """initialize -> capture Mcp-Session-Id -> notifications/initialized."""
        self._rpc_id += 1
        resp = self._post({
            "jsonrpc": "2.0", "id": self._rpc_id, "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "lqabr-text-voice", "version": "rev5"},
            },
        })
        if resp.status_code >= 400:
            raise CRMError(f"MCP initialize failed: HTTP {resp.status_code}: "
                           f"{resp.text[:300]}")
        body = self._parse_body(resp)
        if not body or "error" in body:
            raise CRMError(f"MCP initialize rejected: "
                           f"{json.dumps((body or {}).get('error') or body)[:300]}")
        self._session_id = resp.headers.get("mcp-session-id")
        done = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if done.status_code >= 400:
            raise CRMError(f"MCP initialized-notification failed: "
                           f"HTTP {done.status_code}: {done.text[:300]}")
        obs.log_system("step5.tools",
                       detail=f"connected to the Step 5 MCP server at "
                              f"{self._base_url}")

    def _ensure_session(self) -> None:
        with self._lock:
            if self._session_id is None:
                self._initialize()

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """tools/call with the house retry contract, re-initializing a dropped session."""
        def send() -> requests.Response:
            self._ensure_session()
            self._rpc_id += 1
            return self._post({"jsonrpc": "2.0", "id": self._rpc_id,
                               "method": "tools/call",
                               "params": {"name": name,
                                          "arguments": arguments}})

        def handle(resp: requests.Response) -> Tuple[str, Any]:
            if resp.status_code == 404 and self._session_id:
                with self._lock:
                    self._session_id = None
                return "retry", "MCP session expired (HTTP 404)"
            if resp.status_code in (429, 500, 502, 503, 504):
                return "retry", f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code >= 400:
                raise CRMError(f"MCP tools/call {name} failed: "
                               f"HTTP {resp.status_code}: {resp.text[:500]}")
            return "return", self._unwrap(name, self._parse_body(resp))

        return retrying_call(send, handle, url=f"{self._base_url}#{name}",
                             label=f"MCP tools/call {name}", service="mcp",
                             error_cls=CRMError,
                             max_retries=self._max_retries,
                             backoff_seconds=self._backoff,
                             retry_exceptions=(CRMError,))

    @staticmethod
    def _unwrap(name: str, body: Optional[Dict[str, Any]]) -> Any:
        """The tool's payload out of the JSON-RPC envelope; errors raise CRMError."""
        if body is None:
            raise CRMError(f"MCP tools/call {name} returned no parseable body")
        if "error" in body:
            raise CRMError(f"MCP tools/call {name} error: "
                           f"{json.dumps(body['error'])[:300]}")
        result = body.get("result") or {}
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", [])
                     if isinstance(c, dict)]
            raise CRMError(f"MCP tool {name} reported an error: "
                           f"{' '.join(texts)[:300]}")
        if "structuredContent" in result:
            return result["structuredContent"]
        for chunk in result.get("content", []):
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                text = chunk.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return None

    def get_lead(self, object_id: str) -> Optional[VoiceLead]:
        """One lead by HubSpot object id; None when no such contact exists."""
        lead, _ = self.get_lead_with_extras(object_id)
        return lead

    def get_lead_with_extras(
            self, object_id: str) -> Tuple[Optional[VoiceLead], Dict[str, Any]]:
        """The lead plus extras (lead_context) from one get_lead_profile call."""
        result = self._call_tool("get_lead_profile",
                                 {"objectId": str(object_id)})
        if _is_not_found(result):
            return None, {"lead_context": ""}
        if not isinstance(result, dict):
            raise CRMError(f"MCP get_lead_profile returned an unexpected "
                           f"shape: {str(result)[:200]}")
        flat = _flatten(result)
        return _to_voice_lead(result), {
            "lead_context": str(flat.get("lead_context") or ""),
        }

    def _identity(self, object_id: str,
                  lead: Optional[VoiceLead]) -> Dict[str, Any]:
        """employee_id / company_id / decision_maker_flag — required on every
        write, and the pair the server dedups on, so they identify the record.

        Absent, not falsy: decision_maker is a HubSpot bool, so a legitimate
        False would otherwise read as "missing" and block every write for a
        contact who simply is not the decision maker.
        """
        if lead is None:
            lead = self.get_lead(object_id)
        if lead is None:
            raise CRMError(f"cannot write for {object_id!r}: no such lead, so "
                           f"the required identity fields are unknown")
        identity = {"employee_id": lead.employee_id,
                    "company_id": lead.company_id,
                    "decision_maker_flag": lead.decision_maker}
        missing = [k for k, v in identity.items() if v in (None, "")]
        if missing:
            raise CRMError(f"cannot write for {object_id!r}: {missing} "
                           f"unknown/blank on the current record")
        return identity

    def _push_properties(self, object_id: str, properties: Dict[str, Any],
                         lead: Optional[VoiceLead] = None) -> Dict[str, Any]:
        """One upsert_lead_profile call — the only write path.

        The tool takes FLAT keyword arguments: the identity fields and the
        properties side by side at the top level. It does NOT take an
        objectId/properties wrapper — that shape is rejected with five
        validation errors (objectId and properties unexpected, the three
        identity fields missing-required). Confirmed against the live server
        2026-08-21, and again 2026-09-08 against the research agent's
        working call.

        `last_modfied_voice` keeps HubSpot's typo: that is the real property
        name (verified live — last_modified_voice does not exist).
        """
        arguments = dict(self._identity(object_id, lead))
        arguments.update(properties)
        arguments["last_modfied_voice"] = str(int(time.time() * 1000))
        result = self._call_tool("upsert_lead_profile", arguments)
        if isinstance(result, dict) and str(result.get("status", "")).lower() in (
                "halted", "failed", "error"):
            raise CRMError(f"MCP upsert_lead_profile rejected the write: "
                           f"{json.dumps(result)[:300]}")
        return {"status": "updated", "object_id": object_id, **properties}

    def upsert_lead(self, object_id: str, voice_status: str,
                    lead: Optional[VoiceLead] = None) -> Dict[str, Any]:
        """Write one voice_status transition; the value is checked against the enum."""
        if voice_status not in _VOICE_STATUS_VALUES:
            raise CRMError(f"voice_status {voice_status!r} is not one of "
                           f"the voice_status values "
                           f"{_VOICE_STATUS_VALUES}")
        return self._push_properties(
            object_id, {"voice_status": voice_status}, lead=lead)

    def record_call_outcome(self, object_id: str, outcome: str,
                            lead: Optional[VoiceLead] = None) -> Dict[str, Any]:
        """Rev 5 Step 8: read probability, apply the outcome's event increments, one write."""
        events = _EVENTS_FOR_OUTCOME.get(outcome)
        if events is None:
            raise CRMError(f"unknown Step 7 outcome {outcome!r}; expected one "
                           f"of {tuple(_EVENTS_FOR_OUTCOME)}")

        result: Dict[str, Any] = {"object_id": object_id, "outcome": outcome,
                                  "events": [], "failures": []}
        try:
            current = lead if lead is not None else self.get_lead(object_id)
        except CRMError as exc:
            result["failures"].append(f"crm-error: pre-write read: {exc}")
            result["status"] = "partial"
            return result
        probability = current.probability if current else 0

        for event_type in events:
            probability = apply_event(probability, event_type)
            result["events"].append({"event_type": event_type.value,
                                     "probability": probability})

        properties = {"probability": str(probability),
                      "voice_status": _VOICE_STATUS_FOR_OUTCOME[outcome]}
        try:
            result["upsert"] = self._push_properties(object_id, properties,
                                                     lead=current)
        except CRMError as exc:
            result["failures"].append(f"crm-error: upsert_lead_profile: {exc}")

        result["probability"] = probability
        result["promoted_to_scheduling"] = probability >= SCHEDULING_THRESHOLD
        result["status"] = "partial" if result["failures"] else "ok"
        return result
