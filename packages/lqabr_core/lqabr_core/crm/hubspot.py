"""HubSpot CRM adapter — LQABR's system of record.

Uses the HubSpot CRM v3 REST API with a private-app access token from
Secret Manager (`lqabr-hubspot-access-token`).

Most `lqabr_*` custom properties this used to write/read were deleted from
the portal; two survived (`lqabr_voice_status`, `lqabr_email_status`) and
are real, currently-existing enumeration fields. Field mapping:

    full_name        firstname + lastname
    job_title        jobtitle
    company           company
    email             email, falling back to the custom `email_id` property
                      (some contacts, e.g. enrichment-sourced leads, only
                      have the address there)
    phone             phone
    external ids      employee_id
    decision_maker    decision_maker (not `decision_maker_flag` — that name
                      doesn't exist in this portal)
    opted_out         opted_out
    probability       probability
    voice_status      lqabr_voice_status (label shows as "voice_status" in
                      the HubSpot UI, but the real property name has the
                      `lqabr_` prefix) — written by record_event() for
                      VOICEMAIL_LEFT/CALL_ANSWERED/CALL_ENGAGED
    stage             not stored — derived from probability on read
                      (see stage_for_probability)

`upsert_lead`/`_to_properties` still target the old `lqabr_*` names and are
unused by the Text/Voice Agent; fixing them is out of scope here.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from lqabr_core import observability as obs
from lqabr_core.crm.base import CRMClient, CRMError
from lqabr_core.probability import (SCHEDULING_THRESHOLD, TEXT_VOICE_THRESHOLD,
                                    apply_event, stage_for_probability)
from lqabr_core.secrets import get_secret
from lqabr_core.types import EngagementEvent, EventType, LeadProfile, LeadStage

BASE_URL = "https://api.hubapi.com"

# Every property we read back from HubSpot. Most `lqabr_*` custom properties
# were deleted from this portal — only real, currently-existing fields are
# used. `stage` has no real-field equivalent; it's derived from `probability`
# instead of stored (see stage_for_probability). Two `lqabr_*` properties
# survived the deletion and are still real: `lqabr_voice_status` (label
# "voice_status" in the HubSpot UI) and `lqabr_email_status`.
_PROPERTIES = [
    "firstname", "lastname", "jobtitle", "company", "email", "email_id", "phone",
    "employee_id", "decision_maker", "opted_out", "probability", "lqabr_voice_status",
]

# EventType -> lqabr_voice_status value (enumeration: PENDING, INITIATED,
# COMPLETED, FAILED, VOICEMAIL_LEFT). Only call-related events map to
# something here; email/meeting events leave voice_status untouched.
_VOICE_STATUS_FOR_EVENT = {
    EventType.VOICEMAIL_LEFT: "VOICEMAIL_LEFT",
    EventType.CALL_ANSWERED: "COMPLETED",
    EventType.CALL_ENGAGED: "COMPLETED",
    EventType.CALL_NOT_ANSWERED: "FAILED",
}

# probability -> LeadStage range, mirroring stage_for_probability's thresholds.
_STAGE_RANGES = {
    LeadStage.TEXT_VOICE_OUTREACH: (TEXT_VOICE_THRESHOLD, SCHEDULING_THRESHOLD),
    LeadStage.SCHEDULING: (SCHEDULING_THRESHOLD, None),
}


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
        # A *reference* for the audit log — the Secret Manager name, or a
        # redacted fingerprint when a token was injected directly (tests).
        # Never the token value itself. Same convention as VapiClient.
        self._credential_ref = ("lqabr-hubspot-access-token" if access_token is None
                                else f"injected:{obs.redact(self._token)}")

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        """Every attempt lands on audit_log — status code (or the network
        error), credential reference, attempt number, duration. Rev 5 requires
        this for Steps 3 and 8 ("did this request actually go out, and what
        came back"); before this, only VapiClient emitted it, so a HubSpot
        outage or a bad token produced no audit trail at all.
        """
        url = f"{BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        last_error: Optional[str] = None
        for attempt in range(self._max_retries):
            started = time.perf_counter()
            try:
                resp = self._session.request(method, url, headers=headers, timeout=30, **kwargs)
            except requests.RequestException as exc:
                last_error = str(exc)
                obs.log_http_out(method, url, credential=self._credential_ref,
                                 attempt=attempt + 1, error=last_error,
                                 duration_ms=(time.perf_counter() - started) * 1000,
                                 service="hubspot")
            else:
                obs.log_http_out(method, url, status_code=resp.status_code,
                                 credential=self._credential_ref,
                                 attempt=attempt + 1,
                                 duration_ms=(time.perf_counter() - started) * 1000,
                                 service="hubspot")
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
        probability = int(p.get("probability") or 0)
        return LeadProfile(
            full_name=full_name,
            job_title=p.get("jobtitle"),
            company=p.get("company"),
            # some contacts (e.g. enrichment-sourced leads) hold the address in
            # the custom `email_id` property instead of the standard `email` one
            email=p.get("email") or p.get("email_id"),
            phone=p.get("phone"),
            external_employee_id=p.get("employee_id"),
            stage=stage_for_probability(probability),
            probability=probability,
            object_id=contact.get("id"),
            opted_out=p.get("opted_out") == "true",
            extra={"decision_maker": p.get("decision_maker"),
                   "voice_status": p.get("lqabr_voice_status")},
        )

    # ----------------------------------------------------------------- api
    def upsert_lead(self, profile: LeadProfile) -> LeadProfile:
        existing = self.find_lead_by_email(profile.email) if profile.email else None
        props = self._to_properties(profile)
        if existing and existing.object_id:
            self._request("PATCH", f"/crm/v3/objects/contacts/{existing.object_id}",
                          json={"properties": props})
            profile.object_id = existing.object_id
        else:
            created = self._request("POST", "/crm/v3/objects/contacts", json={"properties": props})
            profile.object_id = created.get("id")
        return profile

    def get_lead(self, object_id: str) -> LeadProfile:
        contact = self._request(
            "GET", f"/crm/v3/objects/contacts/{object_id}",
            params={"properties": ",".join(_PROPERTIES)},
        )
        return self._from_contact(contact)

    def find_lead_by_email(self, email: str) -> Optional[LeadProfile]:
        # filterGroups are OR'd together — some contacts store the address in
        # the standard `email` property, others in the custom `email_id` one.
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]},
                {"filters": [{"propertyName": "email_id", "operator": "EQ", "value": email}]},
            ],
            "properties": _PROPERTIES,
            "limit": 1,
        }
        result = self._request("POST", "/crm/v3/objects/contacts/search", json=body)
        results = result.get("results", [])
        return self._from_contact(results[0]) if results else None

    def find_lead_by_phone(self, phone: str) -> Optional[LeadProfile]:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "phone", "operator": "EQ", "value": phone}]}],
            "properties": _PROPERTIES,
            "limit": 1,
        }
        result = self._request("POST", "/crm/v3/objects/contacts/search", json=body)
        results = result.get("results", [])
        return self._from_contact(results[0]) if results else None

    def leads_in_stage(self, stage: LeadStage, min_probability: int = 0, limit: int = 100) -> List[LeadProfile]:
        """No `stage` field exists in HubSpot — filters by the probability
        range that stage_for_probability() would map back to that stage."""
        lo, hi = _STAGE_RANGES.get(stage, (0, TEXT_VOICE_THRESHOLD))
        lo = max(lo, min_probability)
        filters = [{"propertyName": "probability", "operator": "GTE", "value": str(lo)}]
        if hi is not None:
            filters.append({"propertyName": "probability", "operator": "LT", "value": str(hi)})
        body = {"filterGroups": [{"filters": filters}], "properties": _PROPERTIES, "limit": limit}
        result = self._request("POST", "/crm/v3/objects/contacts/search", json=body)
        return [self._from_contact(c) for c in result.get("results", [])]

    def record_event(self, event: EngagementEvent) -> LeadProfile:
        """Writes `probability` always, plus `lqabr_voice_status` for
        call-related events (see _VOICE_STATUS_FOR_EVENT). The other
        counter fields (lqabr_*_count) this used to also write no longer have
        a real HubSpot property to land in.

        `last_modfied_voice` (real API name, typo included — confirmed via
        the portal's own property definition, not the UI label) is stamped
        only for the same call-related events that write `lqabr_voice_status`
        (2026-08-06, user request): this is the Text/Voice Agent's own
        last-touched marker, so an email or meeting-scheduled event recorded
        through this same shared method must not stamp it.

        Stage isn't stored either; it's derived from probability on read
        (stage_for_probability)."""
        lead = self.get_lead(event.contact_id)
        new_probability = apply_event(lead.probability, event.event_type)

        properties: Dict[str, Any] = {"probability": str(new_probability)}
        voice_status = _VOICE_STATUS_FOR_EVENT.get(event.event_type)
        if voice_status is not None:
            properties["lqabr_voice_status"] = voice_status
            properties["last_modfied_voice"] = str(int(time.time() * 1000))

        self._request("PATCH", f"/crm/v3/objects/contacts/{event.contact_id}",
                      json={"properties": properties})

        lead.probability = new_probability
        lead.stage = stage_for_probability(new_probability)
        return lead

    def set_stage(self, object_id: str, stage: LeadStage, reason: Optional[str] = None) -> None:
        """No-op: stage has no real HubSpot field to write to anymore — it's
        purely derived from probability. Kept only to satisfy the interface."""
        pass
