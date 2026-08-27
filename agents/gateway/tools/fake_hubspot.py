"""Send a properly signed webhook to the gateway, exactly as HubSpot would.

Why this exists: proving the gateway works does not require HubSpot to be able
to reach your machine. The only thing a public tunnel adds is *delivery* — the
signature, the payload shape, the routing decision and the hand-off are all
identical whether the request comes from HubSpot's servers or from here.

So this lets you test the whole chain locally, today, with nothing installed,
and leaves the tunnel/deploy as a separate problem to solve once.

    # gateway on :8080, stub agent on :9001
    python agents/gateway/tools/fake_hubspot.py

    # pick the route
    python agents/gateway/tools/fake_hubspot.py --property email_status --value OPENED
    python agents/gateway/tools/fake_hubspot.py --property decision_maker --value true
    python agents/gateway/tools/fake_hubspot.py --created           # contact.creation

    # the cases that should be REFUSED or dropped
    python agents/gateway/tools/fake_hubspot.py --value BOUNCED     # not the routing condition
    python agents/gateway/tools/fake_hubspot.py --bad-signature     # -> 401
    python agents/gateway/tools/fake_hubspot.py --replay            # stale timestamp -> 401

The signature is computed the way HubSpot documents it:
    base64( HMAC-SHA256( client_secret, METHOD + URI + BODY + TIMESTAMP ) )

Test tool. Not imported by the service, never deployed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import requests

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = GATEWAY_ROOT / "config" / ".env"


def read_env(name: str) -> str:
    """Prefer a real environment variable; fall back to config/.env."""
    value = os.environ.get(name, "")
    if value:
        return value
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Fire a signed HubSpot webhook at the gateway")
    parser.add_argument("--url", default="http://localhost:8080",
                        help="gateway origin (must match LQABR_GATEWAY_PUBLIC_URL)")
    parser.add_argument("--property", default="email_status")
    parser.add_argument("--value", default="OPENED")
    parser.add_argument("--object-id", default="524046551750",
                        help="a real contact id from portal 246777241")
    parser.add_argument("--created", action="store_true",
                        help="send a contact.creation event instead of a property change")
    parser.add_argument("--count", type=int, default=1, help="how many events in the batch")
    parser.add_argument("--bad-signature", action="store_true", help="expect 401")
    parser.add_argument("--replay", action="store_true",
                        help="timestamp an hour old, expect 401")
    args = parser.parse_args()

    secret = read_env("HUBSPOT_APP_SECRET")
    if not secret:
        print("HUBSPOT_APP_SECRET is not set.")
        print(f"Put it in {ENV_FILE} — it is the Client secret from the HubSpot Auth tab.")
        return 1

    path = "/hubspot/events"
    now_ms = int(time.time() * 1000)

    events = []
    for i in range(args.count):
        event = {
            "objectId": int(args.object_id) + i,
            "subscriptionType": "contact.creation" if args.created else "contact.propertyChange",
            "portalId": 246777241,
            "eventId": now_ms + i,
            "occurredAt": now_ms,
            "attemptNumber": 0,
            "changeSource": "CRM",
        }
        if not args.created:
            event["propertyName"] = args.property
            event["propertyValue"] = args.value
        events.append(event)

    body = json.dumps(events)
    timestamp = str(now_ms - 3_600_000 if args.replay else now_ms)
    uri = f"{args.url.rstrip('/')}{path}"

    message = f"POST{uri}{body}{timestamp}"
    signature = base64.b64encode(
        hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()
    if args.bad_signature:
        signature = base64.b64encode(b"this-is-not-the-right-signature").decode()

    print(f"POST {uri}")
    print(f"  signing over : POST + uri + body + {timestamp}")
    print(f"  events       : {len(events)}")
    if not args.created:
        print(f"  {args.property} = {args.value}")
    print()

    try:
        response = requests.post(uri, data=body, timeout=30, headers={
            "Content-Type": "application/json",
            "X-HubSpot-Signature-v3": signature,
            "X-HubSpot-Request-Timestamp": timestamp,
        })
    except requests.RequestException as exc:
        print(f"could not reach the gateway: {exc}")
        print("is uvicorn running on that port?")
        return 1

    print(f"HTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text[:800])

    print()
    if response.status_code == 200:
        summary = response.json()
        if summary.get("routed"):
            print(f"routed {summary['routed']} -> check the stub agent window")
        else:
            print(f"discarded — {summary.get('discards_by_reason')}")
            print("that is a correct outcome if the value is not a routing condition")
    elif response.status_code == 401:
        print("rejected as unauthorised — correct for --bad-signature / --replay.")
        print("Otherwise: HUBSPOT_APP_SECRET or --url does not match "
              "LQABR_GATEWAY_PUBLIC_URL.")
    elif response.status_code == 503:
        print("matched a route but could not hand off — is the stub agent running "
              "on :9001?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
