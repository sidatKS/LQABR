#!/usr/bin/env python3
"""
Push lead_profiles_decision_makers.json into HubSpot.

Adapted to the real file:
  - rows live under the "profiles" key, not at top level
  - phone field is "phone"
  - industry values are free text, NOT in HubSpot's built-in dropdown,
    so industry is skipped (see SKIP_INDUSTRY below)
  - every row shares the same email address

THE EMAIL PROBLEM
-----------------
All 263 records use svktekninjas@gmail.com. Email is HubSpot's unique key
for contacts, so loading as-is gives you ONE contact updated 263 times.

UNIQUE_EMAILS = True rewrites each address using employee_id:
    svktekninjas@gmail.com -> svktekninjas+E00002@gmail.com
Gmail ignores everything after '+', so mail still arrives, but HubSpot
treats each as a separate contact. Good for testing the load; swap in real
addresses before any actual outreach.

Usage:
    set HUBSPOT_TOKEN=pat-na1-...
    python push_leads.py lead_profiles_decision_makers.json --limit 2 --dry-run
    python push_leads.py lead_profiles_decision_makers.json --limit 2
"""

import argparse
import json
import os
import sys
import time
from typing import Optional

import requests

BASE = "https://api.hubapi.com"

# --- switches ---------------------------------------------------------------

# All four custom contact properties now exist in the portal.
INCLUDE_CUSTOM_CONTACT_PROPS = True

# HubSpot's built-in company 'industry' is a fixed enum. Your source values
# ('Oil & gas', 'Data centres', ...) are free text, so they're mapped below.
SKIP_INDUSTRY = False

# your value -> HubSpot's built-in industry enum
INDUSTRY_MAP = {
    "Aerospace":                 "AVIATION_AEROSPACE",
    "Buildings":                 "CONSTRUCTION",
    "Data centres":              "INFORMATION_TECHNOLOGY_AND_SERVICES",
    "Food & beverage":           "FOOD_BEVERAGES",
    "Healthcare":                "HOSPITAL_HEALTH_CARE",
    "Machine building":          "MACHINERY",
    "Mining, metals & minerals": "MINING_METALS",
    "Oil & gas":                 "OIL_ENERGY",
    "Residential":               "REAL_ESTATE",
    "Space":                     "AVIATION_AEROSPACE",
    "Utilities":                 "UTILITIES",
    "Vehicles":                  "AUTOMOTIVE",
}

# Give each row a distinct email via +tag. See note above.
UNIQUE_EMAILS = True

# Internal name of the company frequency property. Must match the portal
# EXACTLY -- internal names cannot be renamed after creation. Verify via
# Manage properties > click the property > internal name shown under the label.
FREQUENCY_PROP = "frequency_pruchase"

TRUTHY = {"yes", "y", "true", "t", "1"}


# --- row -> payload ---------------------------------------------------------

def tag_email(email: str, employee_id: str) -> str:
    if not UNIQUE_EMAILS or not employee_id or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if "+" in local:
        return email
    return f"{local}+{employee_id}@{domain}"


def contact_props(row: dict) -> dict:
    props = {}
    email = str(row.get("email", "")).strip()
    if email:
        props["email"] = tag_email(email, str(row.get("employee_id", "")).strip())

    for src, dest in [("phone", "phone"), ("phone_no", "phone"),
                      ("job_title", "jobtitle")]:
        val = row.get(src)
        if val not in (None, ""):
            props[dest] = str(val).strip()

    # No name fields in the source; use employee_id so records are readable.
    emp = str(row.get("employee_id", "")).strip()
    if emp:
        props["firstname"] = emp

    if INCLUDE_CUSTOM_CONTACT_PROPS:
        if emp:
            props["employee_id"] = emp
        for src, dest in [("email_status", "email_status"),
                          ("voice_status", "voice_status")]:
            val = row.get(src)
            if val not in (None, ""):
                props[dest] = str(val).strip()
        raw = row.get("decision_maker_flag", row.get("decision_maker"))
        if raw not in (None, ""):
            props["decision_maker"] = str(raw).strip().lower() in TRUTHY

    return props


def company_props(row: dict) -> dict:
    cid = str(row.get("company_id", "")).strip()
    if not cid:
        return {}

    # Use a real company name if the source has one; otherwise fall back to
    # the id so the record isn't blank in the UI.
    name = row.get("name") or row.get("company_name")
    props = {"company_id": cid,
             "name": str(name).strip() if name else f"Account {cid}"}

    if not SKIP_INDUSTRY and row.get("industry"):
        raw = str(row["industry"]).strip()
        mapped = INDUSTRY_MAP.get(raw)
        if mapped:
            props["industry"] = mapped
        else:
            print(f"    warning: industry {raw!r} not in INDUSTRY_MAP, skipped")

    rev = row.get("annual_revenue_m")
    if rev not in (None, ""):
        try:
            props["annualrevenue"] = str(int(float(rev) * 1_000_000))
        except ValueError:
            print(f"    warning: bad revenue {rev!r}")

    # NOTE: the internal name in this portal is misspelled 'frequency_pruchase'.
    # Editing a property's LABEL is fine, but the internal name is fixed at
    # creation -- so the code has to match the typo. If you ever delete and
    # recreate it correctly, change FREQUENCY_PROP below.
    freq = row.get("frequency_of_purchase")
    if freq not in (None, ""):
        props[FREQUENCY_PROP] = str(freq).strip()

    return props


# --- API --------------------------------------------------------------------

class HubSpot:
    def __init__(self, token: str, dry_run: bool = False):
        self.dry_run = dry_run
        self.cache: dict[str, str] = {}
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"})

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        for attempt in range(5):
            r = self.s.request(method, f"{BASE}{path}", timeout=30, **kw)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                print(f"    rate limited {wait}s")
                time.sleep(wait)
                continue
            return r
        return r

    def _search(self, obj: str, prop: str, value: str) -> Optional[str]:
        r = self._req("POST", f"/crm/v3/objects/{obj}/search", json={
            "filterGroups": [{"filters": [
                {"propertyName": prop, "operator": "EQ", "value": value}]}],
            "properties": [prop], "limit": 1})
        if r.status_code != 200:
            print(f"    search failed [{r.status_code}] {r.text[:200]}")
            return None
        hits = r.json().get("results", [])
        return hits[0]["id"] if hits else None

    def company(self, props: dict) -> Optional[str]:
        cid = props.get("company_id")
        if not cid:
            return None
        if cid in self.cache:
            print(f"    company {cid} -> {self.cache[cid]} (cached)")
            return self.cache[cid]

        found = self._search("companies", "company_id", cid)
        if found:
            # Update it -- otherwise newly-added properties never get written
            # to companies that were created by an earlier run.
            if self.dry_run:
                print(f"    [dry] update company {cid}: {props}")
            else:
                p = self._req("PATCH", f"/crm/v3/objects/companies/{found}",
                              json={"properties": props})
                if p.status_code == 200:
                    print(f"    company {cid} -> {found} (updated)")
                else:
                    print(f"    company UPDATE FAILED [{p.status_code}] {p.text[:300]}")
            self.cache[cid] = found
            return found

        if self.dry_run:
            print(f"    [dry] create company {props}")
            return "DRY"

        r = self._req("POST", "/crm/v3/objects/companies",
                      json={"properties": props})
        if r.status_code not in (200, 201):
            print(f"    company FAILED [{r.status_code}] {r.text[:300]}")
            return None
        new = r.json()["id"]
        print(f"    company {cid} -> {new} (created)")
        self.cache[cid] = new
        return new

    def contact(self, props: dict) -> Optional[str]:
        email = props.get("email")
        if not email:
            print("    skipped: no email")
            return None

        if self.dry_run:
            print(f"    [dry] upsert contact {props}")
            return "DRY"

        r = self._req("POST", "/crm/v3/objects/contacts",
                      json={"properties": props})
        if r.status_code in (200, 201):
            cid = r.json()["id"]
            print(f"    contact {email} -> {cid} (created)")
            return cid

        if r.status_code == 409:
            existing = self._search("contacts", "email", email)
            if existing:
                p = self._req("PATCH", f"/crm/v3/objects/contacts/{existing}",
                              json={"properties": props})
                ok = p.status_code == 200
                print(f"    contact {email} -> {existing} "
                      f"({'updated' if ok else 'update FAILED'})")
                return existing

        print(f"    contact FAILED [{r.status_code}] {r.text[:300]}")
        return None

    def link(self, contact_id: str, company_id: str) -> None:
        if self.dry_run:
            print(f"    [dry] link {contact_id} -> {company_id}")
            return
        r = self._req("PUT", f"/crm/v4/objects/contacts/{contact_id}"
                             f"/associations/default/companies/{company_id}")
        if r.status_code in (200, 201, 204):
            print(f"    linked {contact_id} -> {company_id}")
        else:
            print(f"    link FAILED [{r.status_code}] {r.text[:300]}")


# --- input ------------------------------------------------------------------

def read_rows(path: str) -> list[dict]:
    """Handles {'profiles': [...]}, a bare list, or {'data': [...]}."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("profiles", "data", "records", "rows", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    raise ValueError("Could not find a list of records in that file")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N rows")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        sys.exit("Set HUBSPOT_TOKEN first")

    rows = read_rows(args.path)
    total = len(rows)
    if args.limit:
        rows = rows[:args.limit]

    print(f"file has {total} rows, processing {len(rows)}")
    print(f"custom contact props: {'ON' if INCLUDE_CUSTOM_CONTACT_PROPS else 'OFF'}")
    print(f"industry: {'SKIPPED' if SKIP_INDUSTRY else 'included'}")
    print(f"unique emails: {'ON (+tag)' if UNIQUE_EMAILS else 'OFF'}")
    if args.dry_run:
        print("DRY RUN -- nothing written")

    hs = HubSpot(token, args.dry_run)
    ok = bad = 0

    for i, row in enumerate(rows, 1):
        print(f"\n[{i}/{len(rows)}] {row.get('employee_id')} @ {row.get('company_id')}")
        comp = hs.company(company_props(row))
        cont = hs.contact(contact_props(row))
        if not cont:
            bad += 1
            continue
        if comp:
            hs.link(cont, comp)
        ok += 1

    print(f"\n{'=' * 46}\nok {ok}   failed {bad}   companies {len(hs.cache)}")


if __name__ == "__main__":
    main()