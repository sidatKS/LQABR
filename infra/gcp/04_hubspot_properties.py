#!/usr/bin/env python3
"""04 — verify the HubSpot contact properties the Email Agent needs.

SCHEMA NOTE: this previously bootstrapped custom properties, first under
a `lqabr_*`-prefixed schema that didn't match the real account, then
under a second guessed schema (email_status/email_sent/email_opened)
that also didn't match. Confirmed live against the real account
(2026-07-23, ldqfingsrv-dev) that everything the Email Agent needs
ALREADY EXISTS — there is nothing left to create:

    employee_id        (string/text)      — pre-existing
    email_id            (string/text)      — standard HubSpot property
    probability         (number/number)    — pre-existing
    lqabr_email_status  (enumeration)      — pre-existing; allowed values
                        are PENDING, SENT, DELIVERED, OPENED, FAILED,
                        BOUNCED (an email delivery-status field, not a
                        pipeline stage — see lqabr_core/crm/hubspot.py)

This script is now a read-only verification check, not a creator —
running it confirms the properties above are still present with the
expected enumeration options, and fails loudly if any are missing.

Auth: reads the private-app token from the environment
(LQABR_HUBSPOT_ACCESS_TOKEN) or Secret Manager (lqabr-hubspot-access-token)
via lqabr_core. Run:

    pip install -e ../../packages/lqabr_core   # once
    python 04_hubspot_properties.py
"""

from __future__ import annotations

import sys

import requests

from lqabr_core.secrets import get_secret

BASE = "https://api.hubapi.com"

EXPECTED_EMAIL_STATUS_OPTIONS = {"PENDING", "SENT", "DELIVERED", "OPENED", "FAILED", "BOUNCED"}
REQUIRED_PROPERTIES = ["employee_id", "email_id", "company_id", "probability", "lqabr_email_status"]


def main() -> int:
    token = get_secret("lqabr-hubspot-access-token")
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(f"{BASE}/crm/v3/properties/contacts", headers=headers, timeout=30)
    resp.raise_for_status()
    props = {p["name"]: p for p in resp.json().get("results", [])}

    missing = [name for name in REQUIRED_PROPERTIES if name not in props]
    if missing:
        print(f"MISSING required properties: {missing}", file=sys.stderr)
        return 1

    status_options = {o["value"] for o in props["lqabr_email_status"].get("options", [])}
    if status_options != EXPECTED_EMAIL_STATUS_OPTIONS:
        print(f"lqabr_email_status options changed: expected {EXPECTED_EMAIL_STATUS_OPTIONS}, "
              f"got {status_options}", file=sys.stderr)
        return 1

    print("04: all required Email Agent properties present and unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
