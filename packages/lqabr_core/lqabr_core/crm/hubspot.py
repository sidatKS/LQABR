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
            contact_id=contact.get("id"),
            opted_out=p.get("opted_out") == "true",
            extra={"decision_maker": p.get("decision_maker"),
                   "voice_status": p.get("lqabr_voice_status")},
        )

    # ----------------------------------------------------------------- api
    def upsert_lead(self, profile: LeadProfile) -> LeadProfile:
        existing = self.find_lead_by_email(profile.email) if profile.email else None
        props = self._to_properties(profile)
        if existing and existing.contact_id:
            self._request("PATCH", f"/crm/v3/objects/contacts/{existing.contact_id}",
                          json={"properties": props})
            profile.contact_id = existing.contact_id
        else:
            created = self._request("POST", "/crm/v3/objects/contacts", json={"properties": props})
            profile.contact_id = created.get("id")
        return profile

    def get_lead(self, contact_id: str) -> LeadProfile:
        contact = self._request(
            "GET", f"/crm/v3/objects/contacts/{contact_id}",
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

    def set_stage(self, contact_id: str, stage: LeadStage, reason: Optional[str] = None) -> None:
        """No-op: stage has no real HubSpot field to write to anymore — it's
        purely derived from probability. Kept only to satisfy the interface."""
        pass
"""HubSpot CRM adapter — LQABR's system of record.

Uses the HubSpot CRM v3/v4 REST API with a private-app access token from
Secret Manager (`lqabr-hubspot-access-token`).

*** SCHEMA NOTE — confirmed against live HubSpot property definitions
(ldqfingsrv-dev, 2026-07-23) via GET /crm/v3/properties/{contacts,companies} ***

Real internal property names differ from the originally-reported field
list in a few places:

    Contacts:  employee_id (string/text), email_id (the custom property
               used for lookups/writes here — per explicit instruction,
               NOT the standard "email" property, even though live data
               was seen under "email" during testing), lqabr_email_status
               (enumeration: PENDING/SENT/DELIVERED/OPENED/FAILED/
               BOUNCED — an email delivery-status field, not a pipeline
               stage), probability (number/number)
    Companies: company_id (string/text), industry (standard enumeration
               with a fixed HubSpot industry list — read-only in
               practice unless writing one of its exact option values),
               annualrevenue (number/number — no underscore),
               frequency_of_purchase (string/text — correctly spelled;
               the "frequnecy_of_purchase" typo was our own error, not
               HubSpot's)

industry/annualrevenue/frequency_of_purchase/company_id live on the
separate Companies object, associated to the Contact — not contact
properties, hence the association-API calls below.

Key design point: lqabr_email_status is a single current-value field
tracking one email's delivery lifecycle (PENDING before first send, SENT
right after send_outreach_email succeeds, DELIVERED/OPENED as Mailgun
webhook events arrive) — NOT a LeadStage/pipeline-stage proxy. There are
no counter properties in this schema at all (no email_sent/email_opened/
call_started/call_completed) — EVENT_COUNTERS is intentionally empty for
now; engagement is tracked purely via this one status value plus
`probability`. voice_status/decision_maker (both confirmed to exist,
under those exact names) are Text/Voice Agent concerns, out of scope here.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from lqabr_core.crm.base import CRMClient, CRMError
from lqabr_core.probability import EVENT_COUNTERS, apply_event, promotion
from lqabr_core.secrets import get_secret
from lqabr_core.types import EngagementEvent, EventType, LeadProfile, LeadStage

BASE_URL = "https://api.hubapi.com"

# Contact properties we read/write.
# firstname/lastname are STANDARD HubSpot contact properties — the email
# greets the lead by first name. employee_id remains the stable internal
# identifier; the company is reached by association, and company_id comes
# from the Company object (no contact company NAME on this schema).
_PROPERTIES = [
    "firstname", "lastname", "jobtitle", "email_id", "phone",
    "employee_id", "lqabr_email_status", "probability",
    *dict.fromkeys(EVENT_COUNTERS.values()),
]

# Company properties we read/write (separate object, via association).
_COMPANY_PROPERTIES = ["company_id", "industry", "annualrevenue", "frequency_of_purchase"]

# lqabr_email_status is a real, confirmed HubSpot enumeration with
# exactly these allowed values — any other string is rejected (HTTP 400).
EMAIL_STATUS_PENDING = "PENDING"
EMAIL_STATUS_SENT = "SENT"

# Mailgun event -> lqabr_email_status. No distinct "clicked" option exists
# in this enum, so a click is recorded as OPENED (a click implies an open).
_EVENT_TYPE_TO_EMAIL_STATUS = {
    EventType.EMAIL_DELIVERED: "DELIVERED",
    EventType.EMAIL_OPENED: "OPENED",
    EventType.EMAIL_CLICKED: "OPENED",
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

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        url = f"{BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        last_error: Optional[str] = None
        for attempt in range(self._max_retries):
            try:
                resp = self._session.request(method, url, headers=headers, timeout=30, **kwargs)
            # `requests.RequestException` is a SUBCLASS of OSError, not a
            # superset: a plain OSError raised below the requests layer
            # (a missing/unreadable TLS CA bundle, socket exhaustion, a
            # DNS failure surfacing from the OS) is NOT caught by
            # `except RequestException` and would escape this retry loop
            # as an unhandled 500. Catch both.
            except (requests.RequestException, OSError) as exc:
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

    # ------------------------------------------------------------ companies
    def _get_associated_company_id(self, contact_id: str) -> Optional[str]:
        """First associated Company object id for this contact, if any."""
        result = self._request(
            "GET", f"/crm/v4/objects/contacts/{contact_id}/associations/companies")
        results = result.get("results", [])
        return str(results[0]["toObjectId"]) if results else None

    def _fetch_company(self, company_id: str) -> Dict[str, Any]:
        contact = self._request(
            "GET", f"/crm/v3/objects/companies/{company_id}",
            params={"properties": ",".join(_COMPANY_PROPERTIES)},
        )
        return contact.get("properties", {})

    def _upsert_company(self, profile: LeadProfile, contact_id: str) -> None:
        """Create/update the associated Company object and (re-)associate
        it with the contact. Only runs if the profile carries any
        company-level data worth writing."""
        props = {k: v for k, v in {
            "company_id": profile.external_company_id,
            "industry": profile.industry,
            "annualrevenue": profile.company_size_revenue,
            "frequency_of_purchase": profile.extra.get("frequency_of_purchase"),
        }.items() if v not in (None, "")}
        if not props:
            return

        existing_company_id = self._get_associated_company_id(contact_id)
        if existing_company_id:
            self._request("PATCH", f"/crm/v3/objects/companies/{existing_company_id}",
                          json={"properties": props})
            return

        created = self._request("POST", "/crm/v3/objects/companies", json={"properties": props})
        company_id = created.get("id")
        if company_id:
            self._request(
                "PUT",
                f"/crm/v4/objects/contacts/{contact_id}/associations/default/companies/{company_id}",
            )

    # ------------------------------------------------------------- mapping
    def _to_properties(self, profile: LeadProfile) -> Dict[str, Any]:
        first, last = _split_name(profile.full_name)
        props: Dict[str, Any] = {
            "firstname": first,
            "lastname": last,
            "jobtitle": profile.job_title,
            "company": profile.company,
            "email_id": profile.email,
            "phone": profile.phone,
            "employee_id": profile.external_employee_id,
            "probability": str(profile.probability),
        }
        return {k: v for k, v in props.items() if v not in (None, "")}

    def _from_contact(self, contact: Dict[str, Any], company_props: Optional[Dict[str, Any]] = None) -> LeadProfile:
        p = contact.get("properties", {})
        c = company_props or {}
        full_name = " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x) or None
        counters = {name: int(p[name]) for name in EVENT_COUNTERS.values() if p.get(name)}
        email_status = p.get("lqabr_email_status") or EMAIL_STATUS_PENDING
        return LeadProfile(
            full_name=full_name,
            job_title=p.get("jobtitle"),
            company=p.get("company"),
            email=p.get("email_id"),
            phone=p.get("phone"),
            industry=c.get("industry"),
            company_size_revenue=c.get("annualrevenue"),
            # source has no equivalent property in this schema — left at
            # LeadProfile's default (LeadSource.CSV) rather than tracked.
            external_employee_id=p.get("employee_id"),
            external_company_id=c.get("company_id"),
            # lqabr_email_status is an email-delivery-status value, not a
            # pipeline stage — PENDING (no email sent yet) maps to
            # PROFILED; anything past that (SENT/DELIVERED/OPENED/etc.)
            # maps to EMAIL_OUTREACH, since that's all this schema can
            # distinguish for the email channel.
            stage=LeadStage.PROFILED if email_status == EMAIL_STATUS_PENDING else LeadStage.EMAIL_OUTREACH,
            probability=int(float(p.get("probability"))) if p.get("probability") else 0,
            hubspot_contact_id=contact.get("id"),
            extra={"counters": counters, "email_status": email_status,
                  "frequency_of_purchase": c.get("frequency_of_purchase")},
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
            # Only stamp PENDING on brand-new contacts — never overwrite
            # an existing contact's email-delivery progress on a re-sync.
            props["lqabr_email_status"] = EMAIL_STATUS_PENDING
            created = self._request("POST", "/crm/v3/objects/contacts", json={"properties": props})
            profile.hubspot_contact_id = created.get("id")
        self._upsert_company(profile, profile.hubspot_contact_id)
        return profile

    def get_lead(self, contact_id: str) -> LeadProfile:
        contact = self._request(
            "GET", f"/crm/v3/objects/contacts/{contact_id}",
            params={"properties": ",".join(_PROPERTIES)},
        )
        company_id = self._get_associated_company_id(contact_id)
        company_props = self._fetch_company(company_id) if company_id else None
        return self._from_contact(contact, company_props)

    def find_lead_by_email(self, email: str) -> Optional[LeadProfile]:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "email_id", "operator": "EQ", "value": email}]}],
            "properties": _PROPERTIES,
            "limit": 1,
        }
        result = self._request("POST", "/crm/v3/objects/contacts/search", json=body)
        results = result.get("results", [])
        if not results:
            return None
        contact = results[0]
        company_id = self._get_associated_company_id(contact["id"])
        company_props = self._fetch_company(company_id) if company_id else None
        return self._from_contact(contact, company_props)

    def leads_in_stage(self, stage: LeadStage, min_probability: int = 0, limit: int = 100) -> List[LeadProfile]:
        # lqabr_email_status is an email-delivery-status field, not a
        # pipeline stage — there is no HubSpot property representing
        # LeadStage directly. PROFILED/INGESTED/EMAIL_OUTREACH all read as
        # "needs a first send" here, i.e. still PENDING. Any other stage
        # has no equivalent in this schema and returns no results.
        if stage not in (LeadStage.PROFILED, LeadStage.INGESTED, LeadStage.EMAIL_OUTREACH):
            return []
        # Real contacts often have lqabr_email_status blank (never
        # touched by this pipeline) rather than literally "PENDING" —
        # treat both as "needs outreach". HubSpot ORs across filterGroups
        # and ANDs within one, so the probability filter (if any) is
        # duplicated into both groups.
        common: List[Dict[str, Any]] = []
        if min_probability > 0:
            common.append({"propertyName": "probability", "operator": "GTE", "value": str(min_probability)})
        filter_groups = [
            {"filters": [{"propertyName": "lqabr_email_status", "operator": "EQ",
                         "value": EMAIL_STATUS_PENDING}, *common]},
            {"filters": [{"propertyName": "lqabr_email_status", "operator": "NOT_HAS_PROPERTY"}, *common]},
        ]

        # HubSpot's Search API caps a single page at 200 results —
        # paginate via the paging.next.after cursor to satisfy `limit`
        # values above that, instead of clamping and silently returning
        # fewer results than the caller asked for.
        leads: List[LeadProfile] = []
        after: Optional[str] = None
        while len(leads) < limit:
            page_size = min(limit - len(leads), 200)
            body: Dict[str, Any] = {"filterGroups": filter_groups, "properties": _PROPERTIES, "limit": page_size}
            if after:
                # HubSpot's docs specify the after cursor must be
                # formatted as an integer, not a string.
                body["after"] = int(after)
            result = self._request("POST", "/crm/v3/objects/contacts/search", json=body)
            # Company data is skipped here (N+1 association calls per lead
            # in a list would be expensive) — leads_in_stage results carry
            # contact-level fields only; call get_lead()/find_lead_by_email()
            # for the full profile including company data on a specific lead.
            leads.extend(self._from_contact(c) for c in result.get("results", []))
            after = result.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        return leads

    def record_event(self, event: EngagementEvent) -> LeadProfile:
        lead = self.get_lead(event.hubspot_contact_id)

        new_probability = apply_event(lead.probability, event.event_type)
        promoted, new_stage = promotion(lead.probability, new_probability)

        props: Dict[str, Any] = {"probability": str(new_probability)}
        new_email_status = _EVENT_TYPE_TO_EMAIL_STATUS.get(event.event_type)
        if new_email_status:
            props["lqabr_email_status"] = new_email_status

        # No counter properties exist in this schema — EVENT_COUNTERS is
        # empty for now, so this is a no-op until/unless one gets added.
        counters = lead.extra.get("counters", {})
        counter_prop = EVENT_COUNTERS.get(event.event_type)
        if counter_prop:
            new_count = counters.get(counter_prop, 0) + 1
            props[counter_prop] = str(new_count)
            counters[counter_prop] = new_count

        self._request("PATCH", f"/crm/v3/objects/contacts/{event.hubspot_contact_id}",
                      json={"properties": props})

        lead.probability = new_probability
        if promoted:
            # In-memory only — no HubSpot property represents cross-agent
            # pipeline stage in this schema yet, so this can't be
            # persisted. Flagged for follow-up once the Text/Voice
            # handoff needs a real field to read this from.
            lead.stage = new_stage
        lead.extra["counters"] = counters
        return lead

    def set_stage(self, contact_id: str, stage: LeadStage, reason: Optional[str] = None) -> None:
        # Only the first-send transition (-> EMAIL_OUTREACH) has a real
        # equivalent in this schema (lqabr_email_status: PENDING -> SENT).
        # Other stages are accepted for interface compatibility but not
        # persisted — no equivalent property exists for them here.
        if stage is not LeadStage.EMAIL_OUTREACH:
            return
        self._request("PATCH", f"/crm/v3/objects/contacts/{contact_id}",
                      json={"properties": {"lqabr_email_status": EMAIL_STATUS_SENT}})
