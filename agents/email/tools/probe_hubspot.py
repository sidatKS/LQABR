#!/usr/bin/env python3
"""Probe HubSpot for the real lead-selection property. READ-ONLY.

Preflight said `object_id` does not exist on contacts and showed the first
eight id-ish candidates, alphabetically — which cuts off before `hs_object_id`,
HubSpot's own built-in record id. This prints the full picture and then tests
the hypothesis rather than assuming it:

    1. every id-ish contact property, untruncated
    2. whether `hs_object_id` exists (it is standard on every HubSpot object)
    3. a few REAL contacts, so you can see what an object id looks like here
    4. whether the agent's search filter actually works against it
    5. whether `email_campaign_complete` has any near-equivalent already

Nothing is written and nothing is sent. Run from the repo root:

    bash agents/email/run_local.sh probe
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parents[1]
SRC = AGENT_DIR / "src"
for _p in (str(REPO_ROOT), str(SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv

    load_dotenv(AGENT_DIR / ".env", override=False)
except ImportError:
    pass

import logging

logging.getLogger("lqabr.secrets").setLevel(logging.ERROR)

import requests  # noqa: E402

from lqabr_core.secrets import get_secret  # noqa: E402

BASE = "https://api.hubapi.com"


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_secret('lqabr-hubspot-access-token')}",
            "Content-Type": "application/json"}


def main() -> int:
    h = _headers()

    # ---------------------------------------------------------- 1. properties
    resp = requests.get(f"{BASE}/crm/v3/properties/contacts", headers=h, timeout=30)
    resp.raise_for_status()
    props = {p["name"]: p for p in resp.json().get("results", [])}
    print(f"\ncontacts: {len(props)} properties\n")

    idish = sorted(n for n in props
                   if n.endswith("_id") or n.endswith("_ids") or "objectid" in n.lower()
                   or n in ("hs_object_id",))
    print("id-ish properties (full list, not truncated):")
    for n in idish:
        print(f"    {n:<44} {props[n].get('type','')}/{props[n].get('fieldType','')}")

    # ------------------------------------------------- 2. the built-in record id
    print("\nhs_object_id — HubSpot's built-in record id:")
    if "hs_object_id" in props:
        p = props["hs_object_id"]
        print(f"    PRESENT   type={p.get('type')} readOnly={p.get('modificationMetadata', {}).get('readOnlyValue')}")
    else:
        print("    ABSENT (unexpected — it is standard on every HubSpot object)")

    # -------------------------------------------------------- 3. real contacts
    print("\nsample contacts (the `id` field IS the object id):")
    sample = requests.get(
        f"{BASE}/crm/v3/objects/contacts",
        headers=h, timeout=30,
        params={"limit": 5, "properties": "email_id,firstname,lastname,lqabr_email_status"})
    sample.raise_for_status()
    rows = sample.json().get("results", [])
    if not rows:
        print("    (no contacts in this portal)")
    for r in rows:
        pr = r.get("properties", {})
        name = " ".join(x for x in (pr.get("firstname"), pr.get("lastname")) if x) or "-"
        print(f"    id={r['id']:<14} email_id={pr.get('email_id') or '-':<32} "
              f"status={pr.get('lqabr_email_status') or '-':<9} {name}")

    # --------------------------------- 4. does the agent's filter work on it?
    print("\ndoes the agent's search filter work with hs_object_id?")
    if rows:
        probe_id = rows[0]["id"]
        body = {"filterGroups": [{"filters": [
            {"propertyName": "hs_object_id", "operator": "EQ", "value": probe_id}]}],
            "properties": ["email_id", "lqabr_email_status"], "limit": 1}
        s = requests.post(f"{BASE}/crm/v3/objects/contacts/search",
                          headers=h, json=body, timeout=30)
        if s.status_code == 200:
            found = s.json().get("results", [])
            ok = found and found[0]["id"] == probe_id
            print(f"    HTTP 200, matched={'YES' if ok else 'NO'} "
                  f"({len(found)} result(s) for id {probe_id})")
            if ok:
                print("\n    => LQABR_HUBSPOT_OBJECT_ID_PROPERTY=hs_object_id works with the")
                print("       agent's existing search, with NO code change. A campaign then")
                print("       targets exactly the contact whose record id you pass.")
        else:
            print(f"    HTTP {s.status_code}: {s.text[:200]}")
            print("    => hs_object_id is not usable as a search filter here.")

    # ---------------------------------------- 5. the campaign-complete column
    print("\ncampaign-complete candidates already on contacts:")
    want = os.environ.get("LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY", "email_campaign_complete")
    if want in props:
        print(f"    {want} EXISTS")
    else:
        near = sorted(n for n in props
                      if "complete" in n.lower()
                      or ("campaign" in n.lower() and n.startswith("lqabr")))
        print(f"    {want} is ABSENT.")
        print(f"    lqabr_* / *complete* properties present: {near or 'none'}")
        print("    => it has to be CREATED (a bool checkbox). It is ours, not a")
        print("       HubSpot standard field, so nothing here can stand in for it.")

    print("\nnext:")
    print("    export LQABR_HUBSPOT_OBJECT_ID_PROPERTY=hs_object_id")
    if rows:
        print(f"    bash agents/email/run_local.sh dryrun {rows[0]['id']}")
    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.HTTPError as exc:
        print(f"\nHubSpot error: {exc}\n", file=sys.stderr)
        raise SystemExit(1)
