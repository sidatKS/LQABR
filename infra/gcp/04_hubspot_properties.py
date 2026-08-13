#!/usr/bin/env python3
"""04 — bootstrap the LQABR custom contact properties in HubSpot. Idempotent:
existing properties are left untouched.

HubSpot is the central lead-profile source; these properties carry the parts
of the 9-pointer profile that don't map to standard contact fields, plus the
pipeline metadata (stage, probability, engagement counters).

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
GROUP = {"name": "lqabr", "label": "LQABR Lead Qualification", "displayOrder": -1}

STAGES = ["ingested", "profiled", "email_outreach", "text_voice_outreach",
          "scheduling", "meeting_scheduled", "unresolved"]

PROPERTIES = [
    # --- 9-pointer fields without standard HubSpot equivalents -----------
    {"name": "lqabr_industry", "label": "LQABR Industry", "type": "string", "fieldType": "text"},
    {"name": "lqabr_company_size_revenue", "label": "LQABR Company Size / Revenue",
     "type": "string", "fieldType": "text"},
    {"name": "lqabr_location", "label": "LQABR Location", "type": "string", "fieldType": "text"},
    {"name": "lqabr_timezone", "label": "LQABR Timezone", "type": "string", "fieldType": "text"},
    {"name": "lqabr_linkedin_url", "label": "LQABR LinkedIn URL", "type": "string", "fieldType": "text"},
    # --- provenance -------------------------------------------------------
    {"name": "lqabr_source", "label": "LQABR Source", "type": "enumeration", "fieldType": "select",
     "options": [{"label": s, "value": s} for s in ("csv", "zoominfo")]},
    {"name": "lqabr_employee_id", "label": "LQABR External Employee ID", "type": "string", "fieldType": "text"},
    {"name": "lqabr_company_id", "label": "LQABR External Company ID", "type": "string", "fieldType": "text"},
    # --- pipeline ----------------------------------------------------------
    {"name": "lqabr_stage", "label": "LQABR Stage", "type": "enumeration", "fieldType": "select",
     "options": [{"label": s, "value": s} for s in STAGES]},
    {"name": "lqabr_stage_reason", "label": "LQABR Stage Reason", "type": "string", "fieldType": "text"},
    {"name": "lqabr_probability", "label": "LQABR Probability", "type": "number", "fieldType": "number"},
    {"name": "lqabr_last_engaged_at", "label": "LQABR Last Engaged At", "type": "string", "fieldType": "text"},
    # --- engagement counters (incremented by webhooks) ---------------------
    {"name": "lqabr_email_delivered_count", "label": "LQABR Emails Delivered", "type": "number", "fieldType": "number"},
    {"name": "lqabr_email_opened_count", "label": "LQABR Emails Opened", "type": "number", "fieldType": "number"},
    {"name": "lqabr_email_clicked_count", "label": "LQABR Email Links Clicked", "type": "number", "fieldType": "number"},
    {"name": "lqabr_sms_delivered_count", "label": "LQABR SMS Delivered", "type": "number", "fieldType": "number"},
    {"name": "lqabr_voicemail_count", "label": "LQABR Voicemails Left", "type": "number", "fieldType": "number"},
    {"name": "lqabr_call_answered_count", "label": "LQABR Calls Answered", "type": "number", "fieldType": "number"},
    {"name": "lqabr_call_engaged_count", "label": "LQABR Calls Engaged (Q&A)", "type": "number", "fieldType": "number"},
    {"name": "lqabr_meeting_count", "label": "LQABR Meetings Scheduled", "type": "number", "fieldType": "number"},
]


def main() -> int:
    token = get_secret("lqabr-hubspot-access-token")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Property group (409 = already exists).
    resp = requests.post(f"{BASE}/crm/v3/properties/contacts/groups", json=GROUP,
                         headers=headers, timeout=30)
    if resp.status_code not in (201, 409):
        print(f"group create failed: HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return 1
    print(f"group lqabr: {'created' if resp.status_code == 201 else 'exists'}")

    created = skipped = 0
    for prop in PROPERTIES:
        body = {**prop, "groupName": "lqabr"}
        resp = requests.post(f"{BASE}/crm/v3/properties/contacts", json=body,
                             headers=headers, timeout=30)
        if resp.status_code == 201:
            created += 1
            print(f"created  {prop['name']}")
        elif resp.status_code == 409:
            skipped += 1
            print(f"exists   {prop['name']}")
        else:
            print(f"FAILED   {prop['name']}: HTTP {resp.status_code}: {resp.text[:300]}",
                  file=sys.stderr)
            return 1
    print(f"04: HubSpot properties ready ({created} created, {skipped} already existed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
