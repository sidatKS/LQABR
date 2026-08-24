"""Rev 5 STEP 4 and STEP 2 — the one leg out, then the two inbound routes."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Optional, Tuple

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from lqabr_core import observability as obs
from lqabr_core.types import VoiceLead


VAPI_BASE_URL = os.environ.get("LQABR_VAPI_BASE_URL", "https://api.vapi.ai")
VAPI_CREDENTIAL_NAME = "lqabr-vapi-api-key"

VAPI_PHONE_NUMBER_ID = os.environ.get("LQABR_VAPI_PHONE_NUMBER_ID", "")
VAPI_ASSISTANT_ID = os.environ.get("LQABR_VAPI_ASSISTANT_ID", "")

SENDER_NAME = os.environ.get("LQABR_SENDER_NAME", "the LQABR team")

LEAD_CONTEXT_MAX_CHARS = int(os.environ.get("LQABR_LEAD_CONTEXT_MAX_CHARS", "1000"))


class VapiError(RuntimeError):
    """A Vapi call failed after retries. Never swallowed into a success."""


def retrying_call(send: Callable[[], requests.Response],
                  handle: Callable[[requests.Response], Tuple[str, Any]],
                  *, url: str, label: str, service: str, error_cls: type,
                  method: str = "POST", max_retries: int = 3,
                  backoff_seconds: float = 1.0,
                  retry_exceptions: tuple = (),
                  log_extra: Optional[Dict[str, Any]] = None) -> Any:
    """The house retry contract: 3 tries, exponential backoff, every attempt audited."""
    extra = log_extra or {}
    last_error: Optional[str] = None
    for attempt in range(max_retries):
        started = time.perf_counter()
        try:
            resp = send()
        except (requests.RequestException, OSError, *retry_exceptions) as exc:
            last_error = str(exc)
            obs.log_http_out(method, url, attempt=attempt + 1, error=last_error,
                             duration_ms=(time.perf_counter() - started) * 1000,
                             service=service, **extra)
        else:
            obs.log_http_out(method, url, status_code=resp.status_code,
                             attempt=attempt + 1,
                             duration_ms=(time.perf_counter() - started) * 1000,
                             service=service, **extra)
            outcome, value = handle(resp)
            if outcome == "return":
                return value
            last_error = value
        if attempt < max_retries - 1:
            time.sleep(backoff_seconds * (2 ** attempt))
    raise error_cls(f"{label} failed after {max_retries} retries: {last_error}")


def cap_lead_context(raw: Optional[str]) -> str:
    """Trim `lead_context` to LEAD_CONTEXT_MAX_CHARS on a word boundary ("" for blank)."""
    text = " ".join((raw or "").split())
    if len(text) <= LEAD_CONTEXT_MAX_CHARS:
        return text
    cut = text[:LEAD_CONTEXT_MAX_CHARS]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def build_call_payload(lead: VoiceLead, lead_context: Optional[str] = None) -> Dict[str, Any]:
    """The POST /call body: destination, assistant id, personalization."""
    if not VAPI_ASSISTANT_ID:
        raise VapiError(
            "LQABR_VAPI_ASSISTANT_ID is unset. The call script now lives only in the "
            "Vapi dashboard (assistant 4f00be12-203d-468f-a11d-f45798165983); the "
            "in-code transient assistant was removed 2026-08-07. Set the env var."
        )
    variables = lead.personalization()
    variables["sender_name"] = SENDER_NAME
    variables["lead_context"] = cap_lead_context(lead_context)
    if lead.object_id:
        variables["object_id"] = str(lead.object_id)
    if lead.employee_id:
        variables["employee_id"] = str(lead.employee_id)

    customer: Dict[str, Any] = {"number": lead.phone_number}
    if lead.full_name:
        customer["name"] = lead.full_name
    payload: Dict[str, Any] = {
        "customer": customer,
        "assistantOverrides": {"variableValues": variables},
    }
    if VAPI_PHONE_NUMBER_ID:
        payload["phoneNumberId"] = VAPI_PHONE_NUMBER_ID
    payload["assistantId"] = VAPI_ASSISTANT_ID
    return payload


class VapiClient:
    """Typed, mockable Vapi adapter; a 2xx is never retried, so no double dials."""

    def __init__(self, api_key: Optional[str] = None,
                 session: Optional[requests.Session] = None,
                 max_retries: int = 3, backoff_seconds: float = 1.0) -> None:
        env_name = VAPI_CREDENTIAL_NAME.upper().replace("-", "_")
        self._key = api_key or os.environ.get(env_name) or ""
        if not self._key:
            raise VapiError(
                f"no Vapi api key: set {env_name} (env-only by design — this "
                "credential is deliberately not read from Secret Manager)")
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._credential_ref = (VAPI_CREDENTIAL_NAME if api_key is None
                                else f"injected:{obs.redact(self._key)}")

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        url = f"{VAPI_BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}

        def send() -> requests.Response:
            return self._session.request(method, url, headers=headers,
                                         timeout=30, **kwargs)

        def handle(resp: requests.Response) -> Tuple[str, Any]:
            if resp.status_code in (429, 500, 502, 503, 504):
                return "retry", f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code >= 400:
                raise VapiError(f"Vapi {method} {path} failed: "
                                f"HTTP {resp.status_code}: {resp.text[:500]}")
            if not resp.text:
                return "return", {}
            try:
                return "return", resp.json()
            except ValueError as exc:
                # A 2xx with a non-JSON body must fail as a VapiError, not
                # escape as a raw decode exception past the error taxonomy.
                raise VapiError(f"Vapi {method} {path} returned a non-JSON "
                                f"2xx body: {resp.text[:200]}") from exc

        return retrying_call(send, handle, url=url, method=method,
                             label=f"Vapi {method} {path}", service="vapi",
                             error_cls=VapiError,
                             max_retries=self._max_retries,
                             backoff_seconds=self._backoff,
                             log_extra={"credential": self._credential_ref})

    def create_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /call — the single outbound telephony leg."""
        return self._request("POST", "/call", json=payload)


_shared_vapi: Optional[VapiClient] = None


def _vapi() -> VapiClient:
    """Lazily-built shared client: importing this module needs no credential."""
    global _shared_vapi
    if _shared_vapi is None:
        _shared_vapi = VapiClient()
    return _shared_vapi


def reset_vapi_client() -> None:
    """Drop the shared client. For tests and credential rotation."""
    global _shared_vapi
    _shared_vapi = None


def place_call(lead: VoiceLead, lead_context: Optional[str] = None) -> Dict[str, Any]:
    """STEP 4 — dial this lead through Vapi and return the call reference."""
    if not lead.phone_number:
        return {"error": "bad-data: contact has no phone number"}
    if lead.opted_out:
        return {"error": "opted-out: contact has opted out of outreach"}
    if not VAPI_PHONE_NUMBER_ID:
        return {"error": "config-error: LQABR_VAPI_PHONE_NUMBER_ID is unset — "
                         "Vapi has no number to dial from"}

    payload = build_call_payload(lead, lead_context=lead_context)
    call = _vapi().create_call(payload)
    call_id = call.get("id")
    obs.bind(call_id=call_id)
    sent_context = payload["assistantOverrides"]["variableValues"]["lead_context"]
    return {
        "status": "initiated",
        "call_id": call_id,
        "to": lead.phone_number,
        "lead_context_chars": len(sent_context),
    }


@asynccontextmanager
async def _lifespan(_: FastAPI):
    obs.configure()
    obs.log_startup(
        "lqabr-text-voice-webhook",
        vapi_phone_number_id_set=bool(VAPI_PHONE_NUMBER_ID),
        vapi_assistant_id_set=bool(VAPI_ASSISTANT_ID),
    )
    if not VAPI_PHONE_NUMBER_ID:
        obs.log_system("config.incomplete", level=logging.ERROR,
                       detail="LQABR_VAPI_PHONE_NUMBER_ID is unset — Step 4 "
                              "cannot place calls")
    yield
    obs.log_shutdown("lqabr-text-voice-webhook")


app = FastAPI(title="LQABR text_voice agent (Rev 5)",
              description="Two routes in, both fed by the Agent Gateway: "
                          "POST /voice_agent/lead (Step 2) and "
                          "POST /voice_agent/vapi_report (Step 6->7).",
              lifespan=_lifespan)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    """Readiness, not liveness: 503 unless this instance can actually dial.

    The Vapi client is built lazily on the first call, so without this check a
    misconfigured instance passes health checks, is marked healthy, takes
    traffic, writes a claim, and only then discovers it has no credential.
    """
    missing = [name for name, value in (
        ("LQABR_VAPI_PHONE_NUMBER_ID", VAPI_PHONE_NUMBER_ID),
        ("LQABR_VAPI_ASSISTANT_ID", VAPI_ASSISTANT_ID),
        ("LQABR_VAPI_API_KEY", os.environ.get(
            VAPI_CREDENTIAL_NAME.upper().replace("-", "_"), "")),
    ) if not value]
    if missing:
        raise HTTPException(status_code=503,
                            detail=f"config incomplete: {', '.join(missing)} unset")
    return {"status": "ok"}


def _correlation_id(request: Request) -> str:
    """Reuse the gateway's correlation id so one lead's trace stays whole."""
    return (request.headers.get("x-correlation-id")
            or request.headers.get("x-request-id")
            or obs.new_correlation_id())


def _extract_object_id(payload: Any) -> Optional[str]:
    """`object_id` from either accepted shape: A2A JSON-RPC envelope or flat body."""
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("params"), dict):
        metadata = payload["params"].get("metadata")
        if isinstance(metadata, dict) and metadata.get("object_id"):
            return str(metadata["object_id"])
    if payload.get("object_id"):
        return str(payload["object_id"])
    return None


@app.post("/voice_agent/lead", status_code=200)
async def lead(request: Request, background: BackgroundTasks) -> Dict[str, Any]:
    """STEP 2 — answer 200 fast, hand off; no business logic inline."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any parse failure is the same 400
        obs.log_http_in("POST", "/voice_agent/lead", 400, error="malformed JSON body")
        raise HTTPException(status_code=400, detail="malformed JSON body")

    object_id = _extract_object_id(payload)
    if not object_id:
        obs.log_http_in("POST", "/voice_agent/lead", 400, error="no object_id in payload",
                        payload_keys=sorted(payload)[:20] if isinstance(payload, dict) else None)
        raise HTTPException(status_code=400, detail="payload carries no object_id")

    correlation_id = _correlation_id(request)
    obs.log_http_in("POST", "/voice_agent/lead", 200, object_id=object_id,
                    correlation_id=correlation_id)

    background.add_task(_handoff_new_lead, object_id, correlation_id)

    result = {"status": "accepted", "object_id": object_id,
              "correlation_id": correlation_id}
    if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
    return result


@app.post("/voice_agent/vapi_report", status_code=200)
async def call_report(request: Request, background: BackgroundTasks) -> Dict[str, Any]:
    """STEP 6 -> 7 — accept end-of-call reports; acknowledge and drop everything else."""
    try:
        envelope = await request.json()
    except Exception:  # noqa: BLE001
        obs.log_http_in("POST", "/voice_agent/vapi_report", 400, error="malformed JSON body")
        raise HTTPException(status_code=400, detail="malformed JSON body")

    # Vapi (posting directly — no gateway in between) always wraps its
    # server messages in a `message` envelope.
    message = envelope.get("message") if isinstance(envelope, dict) else None
    if not isinstance(message, dict):
        message = {}
    message_type = message.get("type")

    if message_type != "end-of-call-report":
        obs.log_http_in("POST", "/voice_agent/vapi_report", 200, message_type=message_type,
                        note="not an end-of-call report")
        return {"status": "ignored", "message_type": message_type}

    call = message.get("call") or {}
    correlation_id = _correlation_id(request)
    obs.log_http_in("POST", "/voice_agent/vapi_report", 200, message_type=message_type,
                    call_id=call.get("id"),
                    ended_reason=message.get("endedReason"),
                    correlation_id=correlation_id)

    background.add_task(_handoff_call_report, message, correlation_id)
    return {"status": "accepted", "call_id": call.get("id"),
            "correlation_id": correlation_id}


def _handoff_new_lead(object_id: str, correlation_id: str) -> None:
    """Step 2's handoff target: run Steps 3 and 4 for this lead."""
    with obs.correlation(correlation_id, object_id=object_id):
        try:
            from . import text_voice
        except ImportError:  # pragma: no cover - direct `uvicorn tools:app`
            import text_voice  # type: ignore
        try:
            result = text_voice.handle_new_lead(object_id)
            if isinstance(result, dict) and result.get("status") != "initiated":
                # "stopped" (dedup, not-qualified) is normal flow — WARNING;
                # only a real error logs at ERROR.
                level = (logging.ERROR if result.get("status") == "error"
                         else logging.WARNING)
                obs.log_process(obs.STEP_GATEWAY_LEAD, result.get("status", "error"),
                                "Step 3/4 did not complete — see reason",
                                level=level, object_id=object_id,
                                reason=result.get("reason"))
        except Exception as exc:  # noqa: BLE001
            obs.log_process(obs.STEP_GATEWAY_LEAD, "error",
                            "unhandled error in the Step 3->4 handoff",
                            level=logging.ERROR, object_id=object_id,
                            error=f"{type(exc).__name__}: {exc}"[:300])


def _handoff_call_report(message: Dict[str, Any], correlation_id: str) -> None:
    """Step 6→7's handoff target: run Steps 7 and 8 for this report."""
    call_id = (message.get("call") or {}).get("id")
    with obs.correlation(correlation_id, call_id=call_id):
        try:
            from . import text_voice
        except ImportError:  # pragma: no cover
            import text_voice  # type: ignore
        try:
            text_voice.handle_call_report(message)
        except Exception as exc:  # noqa: BLE001
            obs.log_process(obs.STEP_GATEWAY_REPORT, "error",
                            "unhandled error in the Step 7->8 handoff",
                            level=logging.ERROR, call_id=call_id,
                            error=f"{type(exc).__name__}: {exc}"[:300])


__all__ = [
    "app", "place_call", "build_call_payload", "cap_lead_context",
    "VapiClient", "VapiError", "reset_vapi_client", "retrying_call",
]
