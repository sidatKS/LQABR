"""Rev 5 STEP 2 and STEP 4 — the two routes in, and the one leg out.

Both inbound routes are fed by the **Agent Gateway** (a separate service, Rev 5
step ②). This agent never talks to HubSpot's webhook or Vapi's webhook
directly: the gateway owns provider authenticity and payload normalisation, so
nothing in this module knows what a HubSpot signature or a Vapi secret looks
like.

    STEP 2   POST /voice_agent/lead
                                 Handed in by the gateway as an A2A JSON-RPC
                                 `message/send` envelope (see
                                 agents/orchestrator/src/a2a_dispatch.py for
                                 the same shape) — the id lives at
                                 `params.metadata.object_id`, HubSpot's own
                                 internal contact id, not the external
                                 employee_id property. A flat
                                 {"object_id": ...} body is also accepted (used
                                 by push_test.sh / manual testing). Corrected
                                 2026-08-05 against a real captured Agent
                                 Gateway request — the previously-documented
                                 flat {"objectId": ...} shape was never what
                                 the real gateway sends. Answer 200 fast, hand
                                 off in-process to the agent. No business logic.
                                 Renamed 2026-08-05 from `/lead` to
                                 `/voice_agent_lead`, then 2026-08-06 to
                                 `/voice_agent/lead` for clearer tracking
                                 across agents sharing the gateway.
    STEP 6→7 POST /voice_agent/vapi_report
                                 Vapi's end-of-call report, forwarded verbatim
                                 by the gateway. A *different* route from
                                 Step 2 — same app, separate handler —
                                 answered 200 and handed off to Step 7.
                                 Renamed 2026-08-05 from `/call-report` to
                                 `/vapi_report`, then 2026-08-06 to
                                 `/voice_agent/vapi_report`.
    STEP 4   POST /call          The only outbound telephony leg in the whole
                                 system: api.vapi.ai. Nothing else in this
                                 repo originates a call.

Run locally:  uvicorn tools:app --port 8082    (from agents/text_voice/src)
Deployed as:  Cloud Run service lqabr-text-voice-webhook

Why both routes answer before doing the work
--------------------------------------------
Rev 5 Step 2 requires the gateway leg to "respond fast, and perform no business
logic itself". Steps 3 and 4 together are two-to-three HubSpot calls plus a
Vapi call; Steps 7 and 8 add a model round-trip. Holding the caller's
connection open for that is how you collect duplicate deliveries — and a retry
mid-Step-4 places a second phone call to a real person. So each route schedules
the work as a FastAPI background task (in-process — a function call, not a
network hop) and returns immediately.

Authentication
--------------
Inbound: none, deliberately, for now. The gateway is the only expected caller
and it is fronted by its own provider verification; if this service needs to
authenticate the gateway later, the slot is the top of each route handler.
Closing the door at the platform level (Cloud Run `--no-allow-unauthenticated`
plus `roles/run.invoker` for the gateway's service account) needs no code here.

Outbound: the Vapi API key is ENV-ONLY (LQABR_VAPI_API_KEY) by decision —
deliberately no Secret Manager fallback for the credential on the dial path
(see VapiClient.__init__ for the live incident behind this). Twilio creds are
not read by this agent at all under Rev 5. Nothing here holds a credential in
code, and the audit log records a credential *reference*, never a value.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from lqabr_core import observability as obs
from lqabr_core.types import VoiceLead

logger = logging.getLogger("lqabr.text_voice.tools")

# --------------------------------------------------------------------------
# Config. Everything swappable is env/config-driven (CLAUDE.md): no hard-coded
# models, URLs or provider specifics.
# --------------------------------------------------------------------------

VAPI_BASE_URL = os.environ.get("LQABR_VAPI_BASE_URL", "https://api.vapi.ai")

# Used as the env var name (LQABR_VAPI_API_KEY) and the audit-log credential
# reference. NOT a Secret Manager lookup — the Vapi key is env-only by design.
VAPI_CREDENTIAL_NAME = "lqabr-vapi-api-key"

# Public base URL of the **Agent Gateway** — NOT of this service.
#
# Step 4 tells Vapi where to deliver the end-of-call report, and that
# instruction rides on POST /call (see build_assistant_config). Under the Rev 5
# topology the report goes to the gateway, which forwards it to this service's
# /voice_agent/vapi_report. So a wrong value here means every call runs and no
# result is ever recorded — the single most expensive misconfiguration in this
# service.
GATEWAY_BASE_URL = os.environ.get("LQABR_GATEWAY_BASE_URL",
                                  os.environ.get("LQABR_WEBHOOK_BASE_URL",
                                                 "http://localhost:8082"))

# The full URL Vapi is told to POST the end-of-call report to. Defaults to a
# `/voice_agent/vapi_report` path on the gateway, but is independently
# overridable: the exact route Vapi hits is the Agent Gateway's to name, not
# ours, and this service should not have to guess it from GATEWAY_BASE_URL
# alone.
# Route history: `/lead` -> `/voice_agent_lead` -> `/voice_agent/lead`,
# `/call-report` -> `/vapi_report` -> `/voice_agent/vapi_report` (renamed
# again 2026-08-06, user request: nest both under a `/voice_agent/` prefix)
# for clearer log/traffic tracking now that other agents share the same
# gateway. The gateway's Vapi-facing route and this service's own handler
# must share the one name end to end.
VAPI_REPORT_CALLBACK_URL = os.environ.get(
    "LQABR_VAPI_REPORT_CALLBACK_URL", f"{GATEWAY_BASE_URL}/voice_agent/vapi_report")

# How long Vapi will wait for that POST to be acknowledged before treating it
# as failed. Vapi's own default is 20s if unset; made explicit and
# config-driven rather than hard-coded per CLAUDE.md.
VAPI_REPORT_TIMEOUT_SECONDS = int(
    os.environ.get("LQABR_VAPI_REPORT_TIMEOUT_SECONDS", "30"))

# Shared secret the Agent Gateway verifies on the inbound end-of-call report:
# the gateway checks the `x-vapi-secret` header against its own
# LQABR_VAPI_WEBHOOK_SECRET. It is attached to the PER-CALL Vapi `server` object
# (see _report_server) so Vapi presents it on the report — it cannot live only
# in the Vapi dashboard, because the per-call server object replaces the
# dashboard assistant's server config entirely (Vapi assigns the report to
# exactly one server). Must equal the gateway's value. Env-only, same policy as
# the other credentials.
VAPI_WEBHOOK_SECRET = os.environ.get("LQABR_VAPI_WEBHOOK_SECRET", "")

# Vapi routing. The phone number id is the caller ID Vapi dials from.
VAPI_PHONE_NUMBER_ID = os.environ.get("LQABR_VAPI_PHONE_NUMBER_ID", "")
# Optional: a dashboard-managed assistant. Unset (the default) means the
# transient assistant built below is sent with the call, which keeps the call
# script in version control instead of in someone's browser tab.
VAPI_ASSISTANT_ID = os.environ.get("LQABR_VAPI_ASSISTANT_ID", "")

SENDER_NAME = os.environ.get("LQABR_SENDER_NAME", "the LQABR team")

# Vapi's voice/model/transcriber providers, config-driven per CLAUDE.md.
VAPI_VOICE_PROVIDER = os.environ.get("LQABR_VAPI_VOICE_PROVIDER", "vapi")
VAPI_VOICE_ID = os.environ.get("LQABR_VAPI_VOICE_ID", "Elliot")
VAPI_CALL_MODEL_PROVIDER = os.environ.get("LQABR_VAPI_CALL_MODEL_PROVIDER", "openai")
VAPI_CALL_MODEL = os.environ.get("LQABR_VAPI_CALL_MODEL", "gpt-4o-mini")
VAPI_MAX_CALL_SECONDS = int(os.environ.get("LQABR_VAPI_MAX_CALL_SECONDS", "300"))


class VapiError(RuntimeError):
    """A Vapi call failed after retries. Never swallowed into a success."""


# ==========================================================================
# STEP 4 — Place the Call
# ==========================================================================

# The Q&A script. Carried over verbatim in substance from the retired
# conversation.py so the outreach copy survived the Twilio→Vapi migration: the
# three questions, the opt-out path and the closing are product decisions, not
# implementation details. Under Rev 5 they are *instructions to Vapi's model*
# rather than a TwiML state machine, because Vapi owns every turn (Step 6) and
# no code in this repo runs mid-call.
#
# {{double braces}} are Vapi/LiquidJS template variables, substituted from
# assistantOverrides.variableValues at call time. Single braces are NOT used —
# nothing here goes through str.format.
ASSISTANT_SYSTEM_PROMPT = """\
You are {{sender_name}}'s outreach assistant for a B2B lead-qualification \
company, calling {{full_name}} ({{job_title}}) at {{company}}, a company in \
the {{industry}} industry.

They recently engaged with an email we sent about how teams in their industry \
qualify leads automatically. This is a short, polite follow-up call.

Your job is to get these three questions answered, in order, one at a time, \
waiting for a real answer before moving on. Never ask two at once. Question 1 \
was already spoken as your first message when the call connected — do NOT \
repeat it unless they clearly did not hear you.

1. "Hi {{first_name}}, this is {{sender_name}}'s assistant calling for \
{{company}}. Do you have a quick minute to answer two short questions? Please \
say yes or no."
2. "Great, thank you! Is {{company}} currently looking at ways to improve how \
you find and qualify new prospects? Please say yes or no."
3. "Perfect. Would it be helpful if we emailed you a calendar link to pick a \
time with our team — mornings or afternoons both work. Please say yes or no."

Rules:
- Ask ALL three questions, strictly in order, exactly one at a time. NEVER \
skip question 2. A single reply — even an emphatic "yes yes" — counts as \
the answer ONLY to the one question you just asked; the next thing you say \
must be the next question in order.
- Wait for their answer to question 1. If they say yes (or anything that is \
not a refusal), ask question 2 exactly as written.
- If their reply is just a greeting ("hello?"), confusion, or they clearly \
did not hear you: re-introduce yourself ONCE in one short sentence by \
repeating question 1, then wait for their yes or no. Do not ask for \
permission more than twice.
- If they say no, or ask to be left alone, at ANY point: thank them warmly, \
say you will not call again, and end the call. Do not attempt to persuade them.
- If they ask to be removed from the list or say "stop", acknowledge it \
explicitly, confirm they will be removed, and end the call.
- The moment they answer question 3 — yes, no, "okay", anything — the call is \
OVER. Say one closing sentence (if they said yes: they will receive an email \
with available times shortly; otherwise: thank them for their time) and call \
the endCall tool in that same turn. Never wait for them to speak again after \
question 3; there is nothing left to ask. (Verified live: without this, the \
call sat open in silence after the final answer until a timeout ended it.)
- Keep every turn to one or two short sentences. This is a phone call, not a \
presentation.
- Never invent product details, pricing, customer names or commitments. If you \
do not know something, say you will have someone follow up by email.
- If you realize you have reached a voicemail or answering system rather than \
a person (a recorded greeting, "no one is available to take your call", an \
instruction to leave a message): follow these three steps IN ORDER, no step \
may be skipped. Step 1: wait silently for the beep/tone after the greeting \
finishes. Step 2: SPEAK this message in full — "Hi {{first_name}}, this is \
{{sender_name}} reaching out regarding {{company}}. We recently shared some \
material on how teams in {{industry}} are qualifying leads automatically, \
and I'd love two minutes of your time. I'll follow up by email. Thank you!" \
Step 3: only after the message has been fully spoken, use the endCall tool. \
NEVER use endCall on a voicemail before you have spoken the message — \
hanging up at the tone without speaking leaves the person NOTHING (verified \
live, twice: once the call sat in 40s of dead air after the message; once \
the call hung up at the tone with the message never delivered — both are \
failures, and skipping the message is the worse one).
- Ending the call is YOUR job, done by calling the endCall tool — the person \
will usually not hang up first. Any time you say a goodbye/closing sentence, \
call endCall in that same turn. Do not keep the line open, ever.
"""
# The voicemail text above is duplicated from ASSISTANT_VOICEMAIL_MESSAGE on
# purpose: that field only plays when Vapi's voicemailDetection fires, and
# verified live (call 019fb7ea..., endedReason=assistant-ended-call) detection
# can miss — a real "no one available" greeting arrived at +37s, nothing
# triggered, and the model — told to "leave the voicemail message" it had
# never been given — could only hang up. Now the model can deliver it itself.

ASSISTANT_FIRST_MESSAGE = (
    "Hi {{first_name}}, this is {{sender_name}}'s assistant calling for "
    "{{company}}. Do you have a quick minute to answer two short questions? "
    "Please say yes or no."
)

# Spoken into a mailbox when Vapi detects one. Mirrors the retired
# VOICEMAIL_TEMPLATE.
ASSISTANT_VOICEMAIL_MESSAGE = (
    "Hi {{first_name}}, this is {{sender_name}} reaching out regarding "
    "{{company}}. We recently shared some material on how teams in "
    "{{industry}} are qualifying leads automatically, and I'd love two minutes "
    "of your time. I'll follow up by email. Thank you!"
)


def _report_server() -> Dict[str, Any]:
    """The per-call Vapi `server` object — the end-of-call report target.

    `assistant.server` is the highest-precedence webhook target, and setting it
    per call means the report comes back to the revision that placed the call.
    This object REPLACES the dashboard assistant's server config wholesale, so
    everything the report path needs must be here:
      - backoffPlan: Vapi's default maxRetries is 0, so one transient delivery
        failure would drop the report for good. Step 8 is idempotent, so a
        redelivery cannot double-count.
        `baseDelaySeconds` is required here too — 2026-08-05, verified live:
        Vapi's docs list it as optional (default 1), but a real POST /call
        with `backoffPlan: {"maxRetries": 2}` and no `baseDelaySeconds` was
        rejected with a 400 citing `baseDelaySeconds must not be greater than
        10` / `must not be less than 0` / `must be a number` all at once —
        the shape NestJS class-validator produces when a field is undefined
        and multiple decorators (@IsNumber/@Min/@Max) all fail on it. The
        live API's enforced behavior wins over the docs here.
      - x-vapi-secret header (when LQABR_VAPI_WEBHOOK_SECRET is set): the shared
        secret the Agent Gateway verifies. A CUSTOM header name on purpose — an
        `Authorization` header here would override credentialId and is a
        documented Vapi anti-pattern.
    """
    server: Dict[str, Any] = {
        "url": VAPI_REPORT_CALLBACK_URL,
        "timeoutSeconds": VAPI_REPORT_TIMEOUT_SECONDS,
        "backoffPlan": {"maxRetries": 2, "baseDelaySeconds": 1},
    }
    if VAPI_WEBHOOK_SECRET:
        server["headers"] = {"x-vapi-secret": VAPI_WEBHOOK_SECRET}
    return server


def build_assistant_config() -> Dict[str, Any]:
    """The transient assistant sent with each call.

    Deliberately does NOT set `analysisPlan`. Vapi would happily produce a
    summary and a success evaluation for us, but Rev 5 Step 7 is explicit that
    the agent's own model reads the transcript and endedReason and decides —
    "it does not rely on a prebuilt Vapi analysisPlan". Paying Vapi to compute
    an analysis we then ignore would be waste, and worse, having two
    disagreeing verdicts on one call in the logs.

    `voicemailDetection` uses Vapi's own provider rather than Twilio's. The
    Twilio AMD path is what produced this project's confirmed bug: its
    AnsweredBy returned "unknown" on every live test call including a real
    55-second conversation, because "unknown" means "detection timed out", not
    "a human answered". Vapi's docs mark the Twilio provider legacy and "prone
    to false positives". We no longer make that human-vs-machine guess at
    connect time at all — Step 7 decides from what was actually said.
    """
    return {
        "firstMessage": ASSISTANT_FIRST_MESSAGE,
        "model": {
            "provider": VAPI_CALL_MODEL_PROVIDER,
            "model": VAPI_CALL_MODEL,
            "messages": [{"role": "system", "content": ASSISTANT_SYSTEM_PROMPT}],
            # The assistant must be able to hang up once the script is done,
            # otherwise every call runs to maxDurationSeconds.
            # `endCallFunctionEnabled` is gone from Vapi's current schema, and
            # tools live under model.tools, NOT assistant.tools — verified
            # live: a top-level assistant.tools gets HTTP 400 "assistant.
            # property tools should not exist" (Vapi validates strictly), and
            # the create-call API reference nests tools inside model.
            "tools": [{"type": "endCall"}],
        },
        "voice": {"provider": VAPI_VOICE_PROVIDER, "voiceId": VAPI_VOICE_ID},
        "voicemailDetection": {"provider": "vapi", "type": "audio"},
        "voicemailMessage": ASSISTANT_VOICEMAIL_MESSAGE,
        # Late-pickup handling, added after three live calls showed the same
        # pattern: the greeting plays at ~+1.4s but the callee's audio only
        # starts ~+30s (they answer after a long ring, hear silence, and the
        # default 30s silence timeout then kills the call before or right as
        # they speak — one call ended 'silence-timed-out' with the callee's
        # answer never captured at all).
        #   - silenceTimeoutSeconds: give a late pickup room to actually talk.
        #   - customer.speech.timeout hook: re-greet every ~10s of silence (up
        #     to 3 times, counter reset once they speak) so someone who
        #     answered into dead air hears a live voice instead of nothing.
        "silenceTimeoutSeconds": 60,
        "hooks": [{
            "on": "customer.speech.timeout",
            "options": {"timeoutSeconds": 10, "triggerMaxCount": 3,
                        "triggerResetMode": "onUserSpeech"},
            "do": [{"type": "say",
                    "exact": ["Hello — this is {{sender_name}}'s assistant "
                              "calling about {{company}}. Can you hear me?"]}],
        }],
        "maxDurationSeconds": VAPI_MAX_CALL_SECONDS,
        # Rev 5 Step 6 lists recordingUrl in the report, so recording is on.
        "artifactPlan": {"recordingEnabled": True},
        # Only the one event Step 7 needs. Subscribing to the default set would
        # POST conversation-update on every turn of every call to a route that
        # exists to receive one report.
        "serverMessages": ["end-of-call-report"],
        # The end-of-call report target: URL, retry policy, and the gateway's
        # x-vapi-secret header when configured (see _report_server).
        "server": _report_server(),
    }


def build_call_payload(lead: VoiceLead) -> Dict[str, Any]:
    """The POST /call body: destination, assistant config, personalization,
    and the report callback URL (Rev 5 Step 4, process substeps 1 and 3)."""
    variables = lead.personalization()
    variables["sender_name"] = SENDER_NAME
    # Carried on the call so Step 8 can resolve the contact from the report
    # directly. Without it the only identifier Vapi guarantees to send back is
    # the dialled number, which costs a CRM search and fails for any contact
    # whose stored number is formatted differently to the one we dialled.
    #
    # 2026-08-06: VoiceLead's field is `contact_id` (renamed from
    # `hubspot_contact_id` at the user's request, to stop this exact class of
    # naming-mismatch bug — a prior attempt at this comment claimed a rename
    # to `object_id` that never actually happened on VoiceLead and crashed
    # with AttributeError on every real dial). The wire key going out here
    # (`contact_id`) matches what `text_voice.py`'s report reader
    # (`_contact_id_for_report`) actually looks for — verified, not assumed.
    if lead.contact_id:
        variables["contact_id"] = str(lead.contact_id)
    if lead.employee_id:
        variables["employee_id"] = str(lead.employee_id)

    payload: Dict[str, Any] = {
        # A human-readable label so a call is findable in Vapi's dashboard by
        # the lead it belongs to.
        "name": f"lqabr-text-voice-{lead.employee_id or lead.contact_id}",
        "customer": {"number": lead.phone_number, "name": lead.full_name or None},
        "assistantOverrides": {"variableValues": variables},
    }
    if VAPI_PHONE_NUMBER_ID:
        payload["phoneNumberId"] = VAPI_PHONE_NUMBER_ID
    if VAPI_ASSISTANT_ID:
        # A dashboard-managed assistant: send only the id, and let the
        # overrides personalize it.
        payload["assistantId"] = VAPI_ASSISTANT_ID
        payload["assistantOverrides"]["server"] = _report_server()
    else:
        payload["assistant"] = build_assistant_config()
    return {k: v for k, v in payload.items() if v is not None}


class VapiClient:
    """Typed, mockable Vapi adapter owning its own retry behaviour.

    Same contract as HubSpotClient per CLAUDE.md: 3 tries, exponential backoff
    on 429/5xx, CRMError-equivalent (VapiError) on final failure, every attempt
    on the audit log with the credential's name and the status code.

    Retries are safe here only because they apply to *failed* requests: a 5xx
    or a connection error from POST /call means Vapi did not accept the call.
    A 2xx is never retried, so a lead is never dialled twice by this client.
    """

    def __init__(self, api_key: Optional[str] = None,
                 session: Optional[requests.Session] = None,
                 max_retries: int = 3, backoff_seconds: float = 1.0) -> None:
        # Vapi's key is ENV-ONLY by decision (2026-07-31): no Secret Manager
        # fallback. `get_secret` is env-first anyway, but its SM fallback was
        # observed live hanging a call for 60s (broken local gcloud auth ->
        # gRPC retry loop) before failing — for the credential that sits
        # directly on the dial path, a missing env var should fail fast and
        # loudly, not time out slowly. HubSpot/Anthropic keep the SM fallback;
        # Twilio is not read by this agent at all (Rev 5 removed it — the
        # Twilio creds in .env exist only for the Vapi dashboard's number
        # import, which never goes through this code).
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
        last_error: Optional[str] = None
        for attempt in range(self._max_retries):
            started = time.perf_counter()
            try:
                resp = self._session.request(method, url, headers=headers,
                                             timeout=30, **kwargs)
            except requests.RequestException as exc:
                last_error = str(exc)
                obs.log_http_out(method, url, credential=self._credential_ref,
                                 attempt=attempt + 1, error=last_error,
                                 duration_ms=(time.perf_counter() - started) * 1000,
                                 service="vapi")
            else:
                obs.log_http_out(method, url, status_code=resp.status_code,
                                 credential=self._credential_ref,
                                 attempt=attempt + 1,
                                 duration_ms=(time.perf_counter() - started) * 1000,
                                 service="vapi")
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                elif resp.status_code >= 400:
                    raise VapiError(f"Vapi {method} {path} failed: "
                                    f"HTTP {resp.status_code}: {resp.text[:500]}")
                else:
                    return resp.json() if resp.text else {}
            # Don't sleep after the final attempt — there is no retry left to
            # wait for, and the caller is blocked on the dial path.
            if attempt < self._max_retries - 1:
                time.sleep(self._backoff * (2 ** attempt))
        raise VapiError(f"Vapi {method} {path} failed after "
                        f"{self._max_retries} retries: {last_error}")

    def create_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /call — the single outbound telephony leg."""
        return self._request("POST", "/call", json=payload)


_shared_vapi: Optional[VapiClient] = None


def _vapi() -> VapiClient:
    """Lazily-built shared client, so importing this module never needs a
    credential (tests, `--help`, a container that only serves /healthz)."""
    global _shared_vapi
    if _shared_vapi is None:
        _shared_vapi = VapiClient()
    return _shared_vapi


def reset_vapi_client() -> None:
    """Drop the shared client. For tests and credential rotation."""
    global _shared_vapi
    _shared_vapi = None


def place_call(lead: VoiceLead) -> Dict[str, Any]:
    """STEP 4 — dial this lead through Vapi and return the call reference.

    Preconditions are the caller's job (Step 3 in text_voice.py) except the
    two that are strictly about this leg: a destination number must exist, and
    the lead must not have opted out. The opt-out check is repeated here on
    purpose. It is the last line of code before a real phone rings, and a
    missing opt-out check is a compliance problem, not a bug — so it is
    enforced at the boundary rather than trusted from upstream.

    Returns {"status": "initiated", "call_id": ...} on success. Raises
    VapiError if Vapi refused the call, which Step 4's caller records rather
    than retries.
    """
    # contact_id — see build_call_payload's comment above for why this is the
    # single field name now (was hubspot_contact_id, briefly and incorrectly
    # object_id).
    if not lead.phone_number:
        return {"error": "bad-data: contact has no phone number",
                "contact_id": lead.contact_id}
    if lead.opted_out:
        return {"error": "opted-out: contact has opted out of outreach",
                "contact_id": lead.contact_id}
    if not VAPI_PHONE_NUMBER_ID:
        return {"error": "config-error: LQABR_VAPI_PHONE_NUMBER_ID is unset — "
                         "Vapi has no number to dial from",
                "contact_id": lead.contact_id}

    payload = build_call_payload(lead)
    call = _vapi().create_call(payload)
    call_id = call.get("id")
    obs.bind(call_id=call_id)
    return {
        "status": "initiated",
        "call_id": call_id,
        "call_status": call.get("status"),
        "contact_id": lead.contact_id,
        "to": lead.phone_number,
        "assistant": "dashboard" if VAPI_ASSISTANT_ID else "transient",
    }


# ==========================================================================
# The app — one service, two routes in
# ==========================================================================

@asynccontextmanager
async def _lifespan(_: FastAPI):
    """Startup/shutdown logging. (Lifespan style — FastAPI deprecated
    @app.on_event, which warned on every test run.)"""
    obs.configure()
    obs.log_startup(
        "lqabr-text-voice-webhook",
        gateway_base_url=GATEWAY_BASE_URL,
        vapi_phone_number_id_set=bool(VAPI_PHONE_NUMBER_ID),
        vapi_assistant=("dashboard" if VAPI_ASSISTANT_ID else "transient"),
    )
    # These are the two misconfigurations that fail silently rather than
    # loudly: no number to dial from, and a report callback URL that does not
    # reach the gateway.
    if not VAPI_PHONE_NUMBER_ID:
        obs.log_system("config.incomplete", level=logging.ERROR,
                       detail="LQABR_VAPI_PHONE_NUMBER_ID is unset — Step 4 "
                              "cannot place calls")
    # Check the value actually sent to Vapi (VAPI_REPORT_CALLBACK_URL), not
    # just the base it derives from — and at ERROR: a localhost callback in a
    # deployed service means every call happens but no report ever arrives,
    # which is the expensive failure named above. (Pre-deploy audit 2026-08-03:
    # same "deployer must remember an env var" class as the APP_MODULE bug.)
    if "localhost" in VAPI_REPORT_CALLBACK_URL or "127.0.0.1" in VAPI_REPORT_CALLBACK_URL:
        obs.log_system("config.suspect", level=logging.ERROR,
                       detail=f"report callback URL is local "
                              f"({VAPI_REPORT_CALLBACK_URL}) — calls will be "
                              "placed but end-of-call reports will never "
                              "arrive; set LQABR_VAPI_REPORT_CALLBACK_URL "
                              "(or LQABR_GATEWAY_BASE_URL) to a public URL")
    yield
    obs.log_shutdown("lqabr-text-voice-webhook")


app = FastAPI(title="LQABR text_voice agent (Rev 5)",
              description="Two routes in, both fed by the Agent Gateway: "
                          "POST /voice_agent/lead (Step 2) and "
                          "POST /voice_agent/vapi_report (Step 6->7).",
              lifespan=_lifespan)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


def _correlation_id(request: Request) -> str:
    """Reuse the gateway's correlation id, or mint one if it did not send one.

    One lead's journey spans two services and two inbound requests separated by
    the call itself. The gateway binds the id first; replacing it here would
    split every trace in half at the service boundary, which is exactly what
    the correlation design exists to prevent.
    """
    return (request.headers.get("x-correlation-id")
            or request.headers.get("x-request-id")
            or obs.new_correlation_id())


def _extract_object_id(payload: Any) -> Optional[str]:
    """Pull `object_id` out of whatever shape the caller actually sends.

    Corrected 2026-08-05 against a real captured Agent Gateway request (not
    guessed): the gateway does NOT POST a flat `{"object_id": ...}` body. It
    speaks the same JSON-RPC 2.0 A2A `message/send` envelope already used
    elsewhere in this repo (see `agents/orchestrator/src/a2a_dispatch.py ::
    A2ADispatcher.send_message`) and buries the id at
    `params.metadata.object_id`:

        {"jsonrpc": "2.0", "id": "...", "method": "message/send",
         "params": {"message": {...}, "metadata": {"object_id": "...", ...}}}

    `object_id` only — no `objectId` camelCase anywhere, per explicit
    instruction. A plain flat `{"object_id": "..."}` body (e.g. a manual
    curl test) is still accepted as a second, simpler shape.
    """
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
    """STEP 2 — the only way in for a new lead.

    Answer 200, hand off. No business logic and no provider knowledge: the
    Agent Gateway has already established that the request originated from
    HubSpot and normalised the payload down to the one field this agent needs.

    Steps 3 and 4 run in the background *after* this response, so a slow CRM or
    a slow Vapi can never turn into a redelivery — and a redelivery is a second
    phone call to a real person.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any parse failure is the same 400
        obs.log_http_in("POST", "/voice_agent/lead", 400, error="malformed JSON body")
        raise HTTPException(status_code=400, detail="malformed JSON body")

    object_id = _extract_object_id(payload)
    if not object_id:
        # A 400 rather than a 200: this is a gateway-contract error, and it has
        # to be visible as a failure to the caller rather than swallowed here.
        obs.log_http_in("POST", "/voice_agent/lead", 400, error="no object_id in payload",
                        payload_keys=sorted(payload)[:20] if isinstance(payload, dict) else None)
        raise HTTPException(status_code=400, detail="payload carries no object_id")

    correlation_id = _correlation_id(request)
    obs.log_http_in("POST", "/voice_agent/lead", 200, object_id=object_id,
                    correlation_id=correlation_id)
    obs.log_process(obs.STEP_GATEWAY_LEAD, "ok", "handing off to the agent",
                    object_id=object_id, correlation_id=correlation_id)

    # In-process handoff: a function call on this worker, not a network hop.
    background.add_task(_handoff_new_lead, object_id, correlation_id)

    # A2A callers (Agent Gateway's JSON-RPC message/send) expect a JSON-RPC
    # response envelope back — A2ADispatcher.send_message() checks for an
    # "error" key and otherwise reads payload.get("result", {}). Echo the
    # same request id and wrap our status in "result" when the caller used
    # that envelope; plain flat callers (push_test.sh, manual curl) keep
    # getting the simple flat body they already expect.
    result = {"status": "accepted", "object_id": object_id,
              "correlation_id": correlation_id}
    if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
    return result


@app.post("/voice_agent/vapi_report", status_code=200)
async def call_report(request: Request, background: BackgroundTasks) -> Dict[str, Any]:
    """STEP 6 -> 7 — Vapi's end-of-call report, forwarded by the gateway.

    A separate route from Step 2. Vapi wraps every server message in a
    `message` envelope and sends several types to the same URL; only
    `end-of-call-report` starts Step 7. Anything else is acknowledged and
    ignored, because a non-2xx would make the sender retry a message we simply
    do not want. The envelope is accepted either wrapped or unwrapped so the
    gateway may forward it verbatim or strip the wrapper.
    """
    try:
        envelope = await request.json()
    except Exception:  # noqa: BLE001
        obs.log_http_in("POST", "/voice_agent/vapi_report", 400, error="malformed JSON body")
        raise HTTPException(status_code=400, detail="malformed JSON body")

    message = envelope.get("message") if isinstance(envelope, dict) else None
    if not isinstance(message, dict):
        message = envelope if isinstance(envelope, dict) else {}
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
    obs.log_process(obs.STEP_GATEWAY_REPORT, "ok", "handing off to Step 7",
                    call_id=call.get("id"), correlation_id=correlation_id)

    background.add_task(_handoff_call_report, message, correlation_id)
    return {"status": "accepted", "call_id": call.get("id"),
            "correlation_id": correlation_id}


# --------------------------------------------------------------- handoffs

# text_voice imports place_call from this module, so importing it at module
# level here would be a cycle. These two thin wrappers defer the import to
# call time, which also keeps `uvicorn tools:app` importable in an environment
# where the model libraries are not installed.

def _handoff_new_lead(object_id: str, correlation_id: str) -> None:
    """Step 2's handoff target: run Steps 3 and 4 for this lead."""
    with obs.correlation(correlation_id, object_id=object_id):
        try:
            from . import text_voice
        except ImportError:  # pragma: no cover - direct `uvicorn tools:app`
            import text_voice  # type: ignore
        try:
            result = text_voice.handle_new_lead(object_id)
            # handle_new_lead() catches its own step 3/4 failures and returns
            # a {"status": "error"/"stopped", "reason": "..."} dict instead of
            # raising — so the `except Exception` below never sees them, and
            # without this, that `reason` (the only place the real failure
            # message lives, e.g. "pre-dial failure: SecretNotFoundError: ...")
            # was silently dropped: nothing ever logged it. 2026-08-05, found
            # while a live Step 4 failure was completely unexplainable in the
            # terminal output.
            if isinstance(result, dict) and result.get("status") not in ("initiated", "callable"):
                obs.log_process(obs.STEP_GATEWAY_LEAD, "error",
                                "Step 3/4 did not complete — see reason",
                                level=logging.ERROR, object_id=object_id,
                                result_status=result.get("status"),
                                reason=result.get("reason"))
        except Exception:  # noqa: BLE001
            # A background task's exception is invisible by default: FastAPI
            # has already returned 200 and there is no client left to tell.
            # This log line is the only trace, so it must always be written.
            obs.log_process(obs.STEP_GATEWAY_LEAD, "error",
                            "unhandled error in the Step 3->4 handoff",
                            level=logging.ERROR, object_id=object_id)


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
        except Exception:  # noqa: BLE001
            obs.log_process(obs.STEP_GATEWAY_REPORT, "error",
                            "unhandled error in the Step 7->8 handoff",
                            level=logging.ERROR, call_id=call_id)


__all__ = [
    "app", "place_call", "build_call_payload", "build_assistant_config",
    "VapiClient", "VapiError", "reset_vapi_client",
]
