"""HubSpot CRM adapter — LQABR's system of record.

Uses the HubSpot CRM v3 REST API with a private-app access token from
Secret Manager (`lqabr-hubspot-access-token`).

Contact property mapping (custom properties are bootstrapped once by
infra/gcp/04_hubspot_properties.py):

    9 pointers                      HubSpot property
    ------------------------------- -------------------------------
    full_name                       firstname + lastname
    job_title                       jobtitle
    company                         company
    email                           email
    phone                           phone
    industry                        lqabr_industry
    company_size_revenue            lqabr_company_size_revenue
    location (+ timezone)           lqabr_location / lqabr_timezone
    linkedin_url                    lqabr_linkedin_url

    pipeline metadata               HubSpot property
    ------------------------------- -------------------------------
    source                          lqabr_source
    external ids                    lqabr_employee_id / lqabr_company_id
    stage                           lqabr_stage
    probability                     lqabr_probability
    engagement counters             lqabr_*_count (see probability.EVENT_COUNTERS)
    last engagement                 lqabr_last_engaged_at
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from lqabr_core.crm.base import CRMClient, CRMError
from lqabr_core.probability import EVENT_COUNTERS, apply_event, promotion
from lqabr_core.secrets import get_secret
from lqabr_core.types import EngagementEvent, LeadProfile, LeadSource, LeadStage

BASE_URL = "https://api.hubapi.com"

# Every property we read back from HubSpot.
_PROPERTIES = [
    "firstname", "lastname", "jobtitle", "company", "email", "phone",
    "lqabr_industry", "lqabr_company_size_revenue", "lqabr_location",
    "lqabr_timezone", "lqabr_linkedin_url", "lqabr_source",
    "lqabr_employee_id", "lqabr_company_id", "lqabr_stage",
    "lqabr_probability", "lqabr_last_engaged_at",
    *EVENT_COUNTERS.values(),
]


def _split_name(full_name: Optional[str]) -> tuple[str, str]:
    if not full_name:
        return "", ""
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


class HubSpotClient(CRMClient):
    """Typed, mockable HubSpot adapter. Owns its retry behavior (3 tries,
    exponential backoff on 429/5xx). Raises CRMError on final failure."""

    def __init__(self, access_token: Optional[str] = None, session: Optional[requests.Session] = None,
                 max_retries: int = 3, backoff_seconds: float = 1.0) -> None:
        self._token = access_token or get_secret("lqabr-hubspot-access-token")
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._backoff = backoff_seconds

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        url = f"{BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        last_error: Optional[str] = None
        for attempt in range(self._max_retries):
            try:
                resp = self._session.request(method, url, headers=headers, timeout=30, **kwargs)
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                elif resp.status_code >= 400:
                    raise CRMError(f"HubSpot {method} {path} failed: HTTP {resp.status_code}: {resp.text[:500]}")
                else:
                    return resp.json() if resp.text else {}
            time.sleep(self._backoff * (2 ** attempt))
        raise CRMError(f"HubSpot {method} {path} failed after {self._max_retries} retries: {last_error}")

    # ------------------------------------------------------------- mapping
    def _to_properties(self, profile: LeadProfile) -> Dict[str, Any]:
        first, last = _split_name(profile.full_name)
        props: Dict[str, Any] = {
            "firstname": first,
            "lastname": last,
            "jobtitle": profile.job_title,
            "company": profile.company,
            "email": profile.email,
            "phone": profile.phone,
            "lqabr_industry": profile.industry,
            "lqabr_company_size_revenue": profile.company_size_revenue,
            "lqabr_location": profile.location,
            "lqabr_timezone": profile.timezone,
            "lqabr_linkedin_url": profile.linkedin_url,
            "lqabr_source": profile.source.value,
            "lqabr_employee_id": profile.external_employee_id,
            "lqabr_company_id": profile.external_company_id,
            "lqabr_stage": profile.stage.value,
            "lqabr_probability": str(profile.probability),
        }
        return {k: v for k, v in props.items() if v not in (None, "")}

    @staticmethod
    def _from_contact(contact: Dict[str, Any]) -> LeadProfile:
        p = contact.get("properties", {})
        full_name = " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x) or None
        counters = {name: int(p[name]) for name in EVENT_COUNTERS.values() if p.get(name)}
        return LeadProfile(
            full_name=full_name,
            job_title=p.get("jobtitle"),
            company=p.get("company"),
            email=p.get("email"),
            phone=p.get("phone"),
            industry=p.get("lqabr_industry"),
            company_size_revenue=p.get("lqabr_company_size_revenue"),
            location=p.get("lqabr_location"),
            timezone=p.get("lqabr_timezone"),
            linkedin_url=p.get("lqabr_linkedin_url"),
            source=LeadSource(p.get("lqabr_source") or "csv"),
            external_employee_id=p.get("lqabr_employee_id"),
            external_company_id=p.get("lqabr_company_id"),
            stage=LeadStage(p.get("lqabr_stage") or "ingested"),
            probability=int(p.get("lqabr_probability") or 0),
            hubspot_contact_id=contact.get("id"),
            extra={"counters": counters},
        )

    # ----------------------------------------------------------------- api
    def upsert_lead(self, profile: LeadProfile) -> LeadProfile:
        existing = self.find_lead_by_email(profile.email) if profile.email else None
        props = self._to_properties(profile)
        if existing and existing.hubspot_contact_id:
            self._request("PATCH", f"/crm/v3/objects/contacts/{existing.hubspot_contact_id}",
                          json={"properties": props})
            profile.hubspot_contact_id = existing.hubspot_contact_id
        else:
            created = self._request("POST", "/crm/v3/objects/contacts", json={"properties": props})
            profile.hubspot_contact_id = created.get("id")
        return profile

    def get_lead(self, contact_id: str) -> LeadProfile:
        contact = self._request(
            "GET", f"/crm/v3/objects/contacts/{contact_id}",
            params={"properties": ",".join(_PROPERTIES)},
        )
        return self._from_contact(contact)

    def find_lead_by_email(self, email: str) -> Optional[LeadProfile]:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": _PROPERTIES,
            "limit": 1,
        }
        result = self._request("POST", "/crm/v3/objects/contacts/search", json=body)
        results = result.get("results", [])
        return self._from_contact(results[0]) if results else None

    def leads_in_stage(self, stage: LeadStage, min_probability: int = 0, limit: int = 100) -> List[LeadProfile]:
        filters = [{"propertyName": "lqabr_stage", "operator": "EQ", "value": stage.value}]
        if min_probability > 0:
            filters.append({"propertyName": "lqabr_probability", "operator": "GTE",
                            "value": str(min_probability)})
        body = {"filterGroups": [{"filters": filters}], "properties": _PROPERTIES, "limit": limit}
        result = self._request("POST", "/crm/v3/objects/contacts/search", json=body)
        return [self._from_contact(c) for c in result.get("results", [])]

    def record_event(self, event: EngagementEvent) -> LeadProfile:
        lead = self.get_lead(event.hubspot_contact_id)
        counter_prop = EVENT_COUNTERS[event.event_type]
        counters = lead.extra.get("counters", {})
        new_count = counters.get(counter_prop, 0) + 1

        new_probability = apply_event(lead.probability, event.event_type)
        promoted, new_stage = promotion(lead.probability, new_probability)

        props = {
            counter_prop: str(new_count),
            "lqabr_probability": str(new_probability),
            "lqabr_last_engaged_at": event.occurred_at
            or datetime.now(timezone.utc).isoformat(),
        }
        if promoted:
            props["lqabr_stage"] = new_stage.value
        self._request("PATCH", f"/crm/v3/objects/contacts/{event.hubspot_contact_id}",
                      json={"properties": props})

        lead.probability = new_probability
        if promoted:
            lead.stage = new_stage
        counters[counter_prop] = new_count
        lead.extra["counters"] = counters
        return lead

    def set_stage(self, contact_id: str, stage: LeadStage, reason: Optional[str] = None) -> None:
        props: Dict[str, Any] = {"lqabr_stage": stage.value}
        if reason:
            props["lqabr_stage_reason"] = reason
        self._request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": props})
