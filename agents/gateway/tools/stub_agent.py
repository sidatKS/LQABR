"""A throwaway stand-in for the Email / Voice agents.

Purpose: prove the HubSpot -> gateway connection end to end before any agent is
deployed. Without something listening, every matched event ends in a routing
error and a 503, which tells you nothing about whether routing worked.

This accepts the same A2A `message/send` call the real agents will, prints what
it received, and acknowledges. It does NOT call HubSpot — it just shows you the
trigger id and the contact id that arrived, which is the whole point of the test.

    python agents/gateway/tools/stub_agent.py            # listens on :9001

Then point both agent URLs at it:

    LQABR_EMAIL_AGENT_URL=http://127.0.0.1:9001/a2a/email
    LQABR_TEXT_VOICE_AGENT_URL=http://127.0.0.1:9001/a2a/voice

Not part of the service. Nothing imports it, and it is never deployed.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

import requests
from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

app = FastAPI(title="LQABR stub agent (test only)")

# Optional pass-through. Set these and the stub prints the hand-off AND forwards
# it to the real agent, returning the real reply verbatim -- so the gateway's
# dispatched_ok / dispatched_failed stays honest. Empty = acknowledge locally,
# which is the original behaviour.
#
#   $env:STUB_FORWARD_EMAIL="https://<email-agent>/lead"
#   $env:STUB_FORWARD_VOICE="https://<voice-agent>/lead"
FORWARD = {
    "email":      os.environ.get("STUB_FORWARD_EMAIL", "").strip(),
    "voice":      os.environ.get("STUB_FORWARD_VOICE", "").strip(),
}

RECEIVED: list = []


@app.post("/a2a/{agent}")
async def receive(agent: str, request: Request) -> Dict[str, Any]:
    raw = await request.body()
    body = json.loads(raw)
    params = body.get("params", {})
    metadata = params.get("metadata", {})
    parts = params.get("message", {}).get("parts", [{}])
    trigger_id = parts[0].get("text") if parts else None

    RECEIVED.append({"agent": agent, **metadata})

    print("\n" + "=" * 62)
    print(f"  {agent.upper()} AGENT WOKEN")
    print("=" * 62)
    print(f"  trigger_id : {trigger_id}")
    object_ids = metadata.get('object_ids')
    if object_ids is not None:
        print(f"  object_ids : {object_ids}")
        print(f"  count      : {len(object_ids)} contacts to fetch (grouped, one industry)")
    else:
        print(f"  object_id  : {metadata.get('object_id')}   <- the contact to fetch")
    if metadata.get('summary_ref_id') is not None:
        print(f"  summary_ref: {metadata.get('summary_ref_id')}   <- blog ticket (read the summary)")
    print(f"  run_id     : {metadata.get('run_id')}")
    print(f"  from       : {metadata.get('source')} v{metadata.get('gateway_version')}")
    print(f"  correlation: x-lqabr-trigger-id = "
          f"{request.headers.get('x-lqabr-trigger-id')}")
    print("-" * 62)
    print(f"  A real agent would now call:")
    if object_ids is not None:
        print(f"    POST /crm/v3/objects/contacts/batch/read  ids={object_ids}")
    else:
        print(f"    GET /crm/v3/objects/contacts/{metadata.get('object_id')}")
    print("=" * 62, flush=True)

    target = FORWARD.get(agent, "")
    if not target:
        # The shape the gateway expects back: JSON-RPC 2.0 result, no error key.
        return {"jsonrpc": "2.0", "id": body.get("id"),
                "result": {"accepted": True, "agent": agent, "trigger_id": trigger_id}}

    print(f"  forwarding to {target}", flush=True)
    try:
        reply = await run_in_threadpool(
            lambda: requests.post(target, data=raw,
                                  headers={"Content-Type": "application/json"},
                                  timeout=30))
    except requests.RequestException as exc:
        print(f"  forward FAILED: {exc}", flush=True)
        print("=" * 62, flush=True)
        return Response(content=json.dumps({"detail": f"forward failed: {exc}"}),
                        status_code=502, media_type="application/json")

    print(f"  real agent replied {reply.status_code}: {reply.text[:300]}", flush=True)
    print("=" * 62, flush=True)
    return Response(content=reply.content, status_code=reply.status_code,
                    media_type=reply.headers.get("content-type", "application/json"))


@app.post("/call-report")
async def call_report(request: Request) -> Dict[str, Any]:
    """Stands in for txtv's /call-report while testing the relay locally.

    The real one is on lqabr-dev-txtv, which is private — a laptop cannot
    reach it. This proves the gateway leg only: secret checked, body forwarded
    unchanged, correlation ids logged.
    """
    body = await request.json()
    message = body.get("message", body)
    call = message.get("call", {}) or {}
    variables = (call.get("assistantOverrides", {}) or {}).get("variableValues", {}) or {}
    transcript = (message.get("artifact", {}) or {}).get("transcript", "")

    RECEIVED.append({"agent": "call-report", "call_id": call.get("id")})

    print("\n" + "=" * 62)
    print("  VAPI CALL REPORT RECEIVED (relayed by the gateway)")
    print("=" * 62)
    print(f"  call_id           : {call.get('id')}")
    print(f"  hubspot_contact_id: {variables.get('hubspot_contact_id')}   <- the lead")
    print(f"  endedReason       : {message.get('endedReason')}")
    print(f"  transcript chars  : {len(transcript)}")
    print(f"  x-vapi-secret seen: {request.headers.get('x-vapi-secret') is not None}")
    print(f"  Authorization seen: {request.headers.get('authorization') is not None}"
          "   (an ID token only exists on Cloud Run)")
    print("-" * 62)
    print("  txtv would now classify the outcome and write it to HubSpot.")
    print("=" * 62, flush=True)

    return {"status": "ok", "call_id": call.get("id")}


@app.get("/received")
def received() -> Dict[str, Any]:
    """Everything this stub has been handed, for eyeballing after a test run."""
    return {"count": len(RECEIVED), "items": RECEIVED}


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok", "service": "stub-agent"}


if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
    print(f"stub agent listening on http://127.0.0.1:{port}", flush=True)
    print("waiting for the gateway to hand something over...", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
