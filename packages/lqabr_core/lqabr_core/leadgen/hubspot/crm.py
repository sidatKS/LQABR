"""The central MCP tool surface — exactly two tools.

  TOOL 1  upsert_lead_profiles  (WRITE)  — Step 5. The ONLY writer to HubSpot.
  TOOL 2  get_lead_profile      (READ)   — Step 6. The shared read path.

Why two and not three (context §7.1, settled — do not reopen): post + update
are one *upsert*, not two operations; validation is a step INSIDE the write,
not a tool; only the read is genuinely separate — different contract, different
consumers (email / voice / scheduler).

Both tools are called IN-PROCESS. Step 4 invoking upsert_lead_profiles is a
Python call, not an API hop and not a network hop. The only network traffic in
this module is to HubSpot itself.

HubSpot mechanics below are the proven ones from the verified 263-lead run
(context §5).
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from lqabr_core.obs import get_obs, utc_now_iso

from .auth import AuthConfigError, TokenError, get_auth_header
from .failures import SystemicFailure, TransportFailure
from .schema import (
    COMPANY_DEDUP_PROPERTY,
    COMPANY_PROPERTIES,
    CONTACT_DEDUP_PROPERTY,
    CONTACT_PROPERTIES,
    LeadProfile,
    LeadProfileRecord,
    PushResult,
    SchemaMismatchError,
    assert_valid,
    clean_optional,
    normalise_profile,
    not_found,
)

DEFAULT_API_BASE = "https://api.hubapi.com"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# B3: a stale or revoked token. Retried ONCE with a forced token refresh.
AUTH_REFRESH_STATUS = {401, 403}


class HubSpotError(RuntimeError):
    """A non-retryable, non-schema HubSpot failure (404 on write, ...)."""


# ---------------------------------------------------------------------------
# HTTP transport — one place that talks to the network, so it is one place to
# audit-log and one place to mock in tests.
# ---------------------------------------------------------------------------


@dataclass
class HubSpotHttp:
    base_url: str | None = None
    timeout: float | None = None
    max_retries: int | None = None
    session: Any = None

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or os.getenv("HUBSPOT_API_BASE", DEFAULT_API_BASE)).rstrip("/")
        self.timeout = self.timeout or float(os.getenv("HUBSPOT_HTTP_TIMEOUT_SECONDS", "30"))
        self.max_retries = (
            self.max_retries
            if self.max_retries is not None
            else int(os.getenv("HUBSPOT_MAX_RETRIES", "3"))
        )
        self.session = self.session or requests

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        lead_ref_id: str | None = None,
        event: str = "hubspot_call",
    ) -> Any:
        """One HubSpot call. Fresh M2M token per call, audit-logged, retried.

        Retries cover THREE failure shapes, not one (review findings B2, B3, B15):

          * retryable HTTP status (429/5xx) — exponential backoff, honouring
            ``Retry-After`` when HubSpot sends it
          * transport exception (connection reset, timeout, DNS) — ``requests``
            RAISES rather than returning a response, so the old status-only
            loop never saw these at all
          * 401/403 — retried exactly once with a FORCED token refresh, which
            is the normal expiry path of the target ``refresh_token`` grant

        Auth misconfiguration is systemic, not per-lead: it is re-raised as
        SystemicFailure so the caller halts instead of blaming the record.
        """
        obs = get_obs()
        url = f"{self.base_url}{path}"
        attempt = 0
        forced_refresh_used = False
        refresh_on_attempt = -1
        last_error: str | None = None

        while attempt <= self.max_retries:
            attempt += 1
            force_refresh = attempt == refresh_on_attempt

            # Step A: a fresh token before EVERY HubSpot call.
            try:
                headers = get_auth_header(force_refresh=force_refresh)
            except (AuthConfigError, TokenError) as exc:
                # No lead is at fault and no lead can succeed. Halt.
                raise SystemicFailure(f"{type(exc).__name__}: {exc}", reason="auth") from exc

            response = None
            try:
                with obs.timed_audit(
                    event,
                    lead_ref_id=lead_ref_id,
                    endpoint=path,
                    method=method,
                    attempt=attempt,
                ) as timer:
                    response = self.session.request(
                        method,
                        url,
                        headers=headers,
                        json=json_body,
                        params=params,
                        timeout=self.timeout,
                    )
                    timer.extra["status"] = getattr(response, "status_code", None)
            except requests.exceptions.RequestException as exc:
                # B2: the retry loop used to key off status_code only, so a
                # connection reset escaped it entirely and burned the lead.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt <= self.max_retries:
                    self._backoff(obs, path, lead_ref_id, attempt, None, last_error)
                    continue
                raise TransportFailure(last_error, endpoint=path) from exc

            status = response.status_code

            # B3: stale/revoked token -> force a refresh and retry once.
            if status in AUTH_REFRESH_STATUS and not forced_refresh_used:
                forced_refresh_used = True
                refresh_on_attempt = attempt + 1
                obs.process.emit(
                    "hubspot_auth_refresh_retry",
                    lead_ref_id=lead_ref_id,
                    endpoint=path,
                    status=status,
                    attempt=attempt,
                )
                continue

            if status in RETRYABLE_STATUS:
                if attempt <= self.max_retries:
                    self._backoff(obs, path, lead_ref_id, attempt, status, None, response)
                    continue
                raise TransportFailure(
                    f"{method} {path} still {status} after {attempt} attempts: "
                    f"{_body_text(response)}",
                    status=status,
                    endpoint=path,
                )

            return response

        # max_retries exhausted on the auth-refresh path.
        raise TransportFailure(
            last_error or f"{method} {path} exhausted {attempt} attempts",
            endpoint=path,
        )

    def _backoff(
        self,
        obs: Any,
        path: str,
        lead_ref_id: str | None,
        attempt: int,
        status: int | None,
        error: str | None = None,
        response: Any = None,
    ) -> None:
        """Exponential backoff, honouring Retry-After when HubSpot sends it (B15)."""
        backoff = min(2 ** (attempt - 1), 8) + random.uniform(0, 0.25)
        retry_after = _retry_after_seconds(response)
        if retry_after is not None:
            backoff = retry_after
        obs.process.emit(
            "hubspot_retry",
            lead_ref_id=lead_ref_id,
            endpoint=path,
            status=status,
            error=error,
            attempt=attempt,
            backoff_s=round(backoff, 2),
            retry_after_honoured=retry_after is not None,
        )
        time.sleep(backoff)


_DEFAULT_HTTP: HubSpotHttp | None = None


def _http(http: HubSpotHttp | None = None) -> HubSpotHttp:
    global _DEFAULT_HTTP
    if http is not None:
        return http
    if _DEFAULT_HTTP is None:
        _DEFAULT_HTTP = HubSpotHttp()
    return _DEFAULT_HTTP


def set_http(http: HubSpotHttp | None) -> None:
    """Inject a transport (tests, or a shared session)."""
    global _DEFAULT_HTTP
    _DEFAULT_HTTP = http


# ---------------------------------------------------------------------------
# errors/schema_mismatch.jsonl — kept, not dropped, not inserted
# ---------------------------------------------------------------------------


SCHEMA_MISMATCH_FILE = "schema_mismatch.jsonl"
TRANSPORT_FAILURE_FILE = "transport_failures.jsonl"
UNRESOLVED_FILE = "unresolved.jsonl"


def _errors_path(filename: str = SCHEMA_MISMATCH_FILE) -> Path:
    directory = Path(os.getenv("LQABR_ERRORS_DIR", "errors"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def record_failure(
    profile: LeadProfile,
    lead_ref_id: str,
    reasons: list[str],
    source: str = "validation",
    filename: str = SCHEMA_MISMATCH_FILE,
    **extra: Any,
) -> Path:
    """Append the offending record — WITH its lead_ref_id — to an error file.

    B16: timestamps are UTC ISO, matching every log line, so the error files
    correlate with the audit log without doing timezone arithmetic by hand.
    B6: ``extra`` carries contact_hs_id / company_hs_id / stage_reached, so a
    partially-written lead is visible in the record rather than invisible.
    """
    obs = get_obs()
    path = _errors_path(filename)
    entry = {
        "ts": utc_now_iso(),
        "run_id": obs.run_id,
        "lead_ref_id": lead_ref_id,
        "source": source,
        "reasons": reasons,
        "record": profile.to_dict() if isinstance(profile, LeadProfile) else str(profile),
    }
    entry.update({key: value for key, value in extra.items() if value is not None})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    obs.process.emit(
        "failure_recorded",
        lead_ref_id=lead_ref_id,
        source=source,
        reasons=reasons,
        file=str(path),
        **{key: value for key, value in extra.items() if value is not None},
    )
    return path


def record_schema_mismatch(
    profile: LeadProfile,
    lead_ref_id: str,
    reasons: list[str],
    source: str = "validation",
    **extra: Any,
) -> Path:
    """Bad record: wrong data, or HubSpot rejected the payload. Kept, not dropped."""
    return record_failure(
        profile, lead_ref_id, reasons, source=source, filename=SCHEMA_MISMATCH_FILE, **extra
    )


def record_transport_failure(
    profile: LeadProfile,
    lead_ref_id: str,
    reasons: list[str],
    **extra: Any,
) -> Path:
    """Network/dependency failure — NOT a schema mismatch (B1).

    Kept in its own file so "263 leads failed validation" can never again mean
    "one env var was missing" or "HubSpot was down for 40 seconds".
    """
    return record_failure(
        profile, lead_ref_id, reasons, source="transport",
        filename=TRANSPORT_FAILURE_FILE, **extra
    )


def record_unresolved(entries: list[dict[str, Any]]) -> Path | None:
    """B14: persist Step 3's unresolved leads instead of only logging a count."""
    if not entries:
        return None
    obs = get_obs()
    path = _errors_path(UNRESOLVED_FILE)
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(
                json.dumps(
                    {"ts": utc_now_iso(), "run_id": obs.run_id, "source": "unresolved", **entry},
                    default=str,
                    ensure_ascii=False,
                )
                + "\n"
            )
    obs.process.emit("unresolved_persisted", count=len(entries), file=str(path))
    return path


# ---------------------------------------------------------------------------
# search / create / update primitives
# ---------------------------------------------------------------------------


def _search_by_property(
    object_type: str,
    property_name: str,
    value: str,
    properties: tuple[str, ...],
    *,
    http: HubSpotHttp | None = None,
    lead_ref_id: str | None = None,
) -> dict[str, Any] | None:
    """POST /crm/v3/objects/{object_type}/search — filter property EQ.

    B13: asks for TWO results, not one. ``employee_id`` and ``company_id`` are
    plain custom text properties and HubSpot enforces no uniqueness on them, so
    duplicates are possible. Taking results[0] silently is fine; taking it
    *invisibly* is not — a second match is logged with both HubSpot ids.
    """
    body = {
        "filterGroups": [
            {"filters": [{"propertyName": property_name, "operator": "EQ", "value": value}]}
        ],
        "properties": list(properties),
        "limit": 2,
    }
    response = _http(http).request(
        "POST",
        f"/crm/v3/objects/{object_type}/search",
        json_body=body,
        lead_ref_id=lead_ref_id,
        event=f"hubspot_search_{object_type}",
    )
    if response.status_code != 200:
        raise HubSpotError(
            f"search {object_type} by {property_name} failed "
            f"({response.status_code}): {_body_text(response)}"
        )
    results = (response.json() or {}).get("results") or []
    if len(results) > 1:
        get_obs().process.emit(
            "duplicate_dedup_key",
            lead_ref_id=lead_ref_id,
            object_type=object_type,
            dedup_property=property_name,
            value=value,
            hs_ids=[str(item.get("id")) for item in results],
            note="more than one HubSpot record carries this dedup key; patching the first",
        )
    return results[0] if results else None


def _create_or_update(
    object_type: str,
    existing_id: str | None,
    properties: dict[str, Any],
    *,
    http: HubSpotHttp | None = None,
    lead_ref_id: str | None = None,
) -> tuple[str, str]:
    """post + update are ONE upsert — create-if-absent, patch-if-present."""
    if existing_id:
        response = _http(http).request(
            "PATCH",
            f"/crm/v3/objects/{object_type}/{existing_id}",
            json_body={"properties": properties},
            lead_ref_id=lead_ref_id,
            event=f"hubspot_update_{object_type}",
        )
        action = "update"
    else:
        response = _http(http).request(
            "POST",
            f"/crm/v3/objects/{object_type}",
            json_body={"properties": properties},
            lead_ref_id=lead_ref_id,
            event=f"hubspot_create_{object_type}",
        )
        action = "create"

    if response.status_code in (400, 409, 422):
        # HubSpot rejected the payload: invalid industry option, non-numeric
        # revenue, and friends. This is a schema mismatch, not a crash.
        raise SchemaMismatchError(
            [f"hubspot {action} {object_type} {response.status_code}: {_body_text(response)}"],
            lead_ref_id=lead_ref_id,
            source="hubspot",
        )
    if response.status_code not in (200, 201):
        raise HubSpotError(
            f"{action} {object_type} failed ({response.status_code}): {_body_text(response)}"
        )

    object_id = str((response.json() or {}).get("id"))
    return object_id, action


def _associate(
    contact_hs_id: str,
    company_hs_id: str,
    *,
    http: HubSpotHttp | None = None,
    lead_ref_id: str | None = None,
) -> bool:
    """PUT /crm/v4/objects/contacts/{id}/associations/default/companies/{id}."""
    response = _http(http).request(
        "PUT",
        f"/crm/v4/objects/contacts/{contact_hs_id}/associations/default/companies/{company_hs_id}",
        lead_ref_id=lead_ref_id,
        event="hubspot_associate",
    )
    if response.status_code not in (200, 201, 204):
        raise HubSpotError(
            f"associate contact {contact_hs_id} -> company {company_hs_id} failed "
            f"({response.status_code}): {_body_text(response)}"
        )
    return True


def _body_text(response: Any) -> str:
    try:
        return str(response.text)[:500]
    except Exception:  # pragma: no cover
        return "<unreadable>"


def _retry_after_seconds(response: Any) -> float | None:
    """B15: HubSpot's Retry-After on a 429 beats our guessed backoff."""
    if response is None:
        return None
    try:
        raw = (getattr(response, "headers", None) or {}).get("Retry-After")
        if raw is None:
            return None
        return max(0.0, min(float(raw), 60.0))
    except (TypeError, ValueError):  # pragma: no cover - a date-form Retry-After
        return None


# ===========================================================================
# TOOL 1 — upsert_lead_profiles  (WRITE)
# ===========================================================================


def upsert_lead_profiles(
    profile: LeadProfile,
    lead_ref_id: str,
    *,
    http: HubSpotHttp | None = None,
) -> PushResult:
    """Validate one profile, then create-or-update Company + Contact + associate.

    Step 5. Called once per record by Step 4, in-process.

    Failure handling is three-way (review finding B1) — the caller can tell a
    bad record from a dead dependency:

      bad record / HubSpot 4xx -> errors/schema_mismatch.jsonl, status="failed"
      transport / 5xx exhausted -> errors/transport_failures.jsonl, status="failed"
      auth misconfigured        -> SystemicFailure RAISED, the run halts

    Order note (B6): the COMPANY is upserted before the Contact. If the second
    write is rejected the leftover is an unassociated Company, not an orphan
    Contact sitting in a lead pipeline. Whatever is written is recorded on the
    error entry (``contact_hs_id`` / ``company_hs_id`` / ``stage_reached``), so
    a partial write is visible rather than invisible.
    """
    obs = get_obs()
    result = PushResult(
        lead_ref_id=lead_ref_id,
        employee_id=getattr(profile, "employee_id", ""),
        company_id=getattr(profile, "company_id", ""),
    )
    stage = "validate"

    try:
        # 1. validate — baseline only, no network.
        #    B12: normalise first, so a consumer of this shared tool is not
        #    rejected for an industry the writer would have uppercased anyway.
        profile = normalise_profile(profile)
        assert_valid(profile, lead_ref_id=lead_ref_id)
        obs.process.emit(
            "validate_pass",
            lead_ref_id=lead_ref_id,
            employee_id=profile.employee_id,
            company_id=profile.company_id,
        )

        # 2. company upsert — dedup on company_id
        stage = "company_upsert"
        existing_company = _search_by_property(
            "companies",
            COMPANY_DEDUP_PROPERTY,
            profile.company_id,
            COMPANY_PROPERTIES,
            http=http,
            lead_ref_id=lead_ref_id,
        )
        company_hs_id, company_action = _create_or_update(
            "companies",
            existing_company.get("id") if existing_company else None,
            profile.to_company_properties(),
            http=http,
            lead_ref_id=lead_ref_id,
        )
        result.company_hs_id = company_hs_id
        result.company_action = company_action
        obs.process.emit(
            "company_upserted",
            lead_ref_id=lead_ref_id,
            action=company_action,
            company_hs_id=company_hs_id,
            company_id=profile.company_id,
        )

        # 3. contact upsert — dedup on employee_id
        stage = "contact_upsert"
        existing_contact = _search_by_property(
            "contacts",
            CONTACT_DEDUP_PROPERTY,
            profile.employee_id,
            CONTACT_PROPERTIES,
            http=http,
            lead_ref_id=lead_ref_id,
        )
        contact_hs_id, contact_action = _create_or_update(
            "contacts",
            existing_contact.get("id") if existing_contact else None,
            profile.to_contact_properties(),
            http=http,
            lead_ref_id=lead_ref_id,
        )
        result.contact_hs_id = contact_hs_id
        result.contact_action = contact_action
        obs.process.emit(
            "contact_upserted",
            lead_ref_id=lead_ref_id,
            action=contact_action,
            contact_hs_id=contact_hs_id,
            employee_id=profile.employee_id,
        )

        # 4. associate
        stage = "associate"
        result.associated = _associate(
            contact_hs_id, company_hs_id, http=http, lead_ref_id=lead_ref_id
        )
        obs.process.emit(
            "contact_company_associated",
            lead_ref_id=lead_ref_id,
            contact_hs_id=contact_hs_id,
            company_hs_id=company_hs_id,
        )

        result.status = "pushed"
        return result

    except SystemicFailure:
        # No lead is at fault and no lead can succeed. Do NOT write a per-lead
        # error record — that is exactly the mislabelling B1 was about.
        obs.process.emit(
            "upsert_halted_systemic", lead_ref_id=lead_ref_id, stage_reached=stage
        )
        raise

    except SchemaMismatchError as exc:
        record_schema_mismatch(
            profile,
            lead_ref_id,
            exc.reasons,
            source=exc.source,
            stage_reached=stage,
            contact_hs_id=result.contact_hs_id,
            company_hs_id=result.company_hs_id,
        )
        result.status = "failed"
        result.failure_kind = "record"
        result.reasons = exc.reasons
        return result

    except TransportFailure as exc:
        reasons = [f"TransportFailure: {exc}"]
        record_transport_failure(
            profile,
            lead_ref_id,
            reasons,
            stage_reached=stage,
            contact_hs_id=result.contact_hs_id,
            company_hs_id=result.company_hs_id,
        )
        obs.process.emit("upsert_transport_failed", lead_ref_id=lead_ref_id, reasons=reasons)
        result.status = "failed"
        result.failure_kind = "transport"
        result.reasons = reasons
        return result

    except Exception as exc:  # unexpected — never crash the run, but don't mislabel it
        reasons = [f"{type(exc).__name__}: {exc}"]
        record_transport_failure(
            profile,
            lead_ref_id,
            reasons,
            stage_reached=stage,
            contact_hs_id=result.contact_hs_id,
            company_hs_id=result.company_hs_id,
        )
        obs.process.emit("upsert_failed", lead_ref_id=lead_ref_id, reasons=reasons)
        result.status = "failed"
        result.failure_kind = "transport"
        result.reasons = reasons
        return result


# ===========================================================================
# TOOL 2 — get_lead_profile  (READ)
# ===========================================================================


def get_lead_profile(
    employee_id: str | None = None,
    email: str | None = None,
    *,
    http: HubSpotHttp | None = None,
) -> LeadProfileRecord:
    """Read a lead's current HubSpot state. Read-only — never writes.

    Step 6. Called by the email / voice / scheduler agents, NOT by the
    lead_profile agent.

    ``employee_id`` is the primary key. ``email`` searches the CUSTOM
    ``email_id`` property, not HubSpot's standard ``email``.

    Returns the 9-field LeadProfile plus contact_hs_id / company_hs_id on a
    wrapper, so a consumer can write status back without re-searching.
    """
    obs = get_obs()

    if not employee_id and not email:
        raise ValueError("get_lead_profile requires employee_id or email")

    lookup_property = CONTACT_DEDUP_PROPERTY if employee_id else "email_id"
    lookup_value = employee_id or email
    obs.process.emit("lead_lookup", lookup_property=lookup_property, lookup_value=lookup_value)

    contact = _search_by_property(
        "contacts", lookup_property, lookup_value, CONTACT_PROPERTIES, http=http
    )
    if not contact:
        obs.process.emit("lead_lookup_not_found", lookup_property=lookup_property, lookup_value=lookup_value)
        return not_found()

    contact_hs_id = str(contact.get("id"))
    contact_props = contact.get("properties") or {}

    # associated company
    company_hs_id: str | None = None
    company_props: dict[str, Any] = {}
    company_resolved = True
    warnings: list[str] = []

    assoc = _http(http).request(
        "GET",
        f"/crm/v4/objects/contacts/{contact_hs_id}/associations/companies",
        event="hubspot_read_associations",
    )
    if assoc.status_code == 200:
        results = (assoc.json() or {}).get("results") or []
        if results:
            company_hs_id = str(results[0].get("toObjectId") or results[0].get("id"))
            if len(results) > 1:
                warnings.append(
                    f"contact {contact_hs_id} is associated with {len(results)} companies; "
                    "returning the first"
                )
        else:
            company_resolved = False
            warnings.append(f"contact {contact_hs_id} has no associated company")
    else:
        company_resolved = False
        warnings.append(f"association read failed ({assoc.status_code})")

    if company_hs_id:
        company = _http(http).request(
            "GET",
            f"/crm/v3/objects/companies/{company_hs_id}",
            params={"properties": ",".join(COMPANY_PROPERTIES)},
            event="hubspot_read_company",
        )
        if company.status_code == 200:
            company_props = (company.json() or {}).get("properties") or {}
        else:
            # B9: previously this fell through silently and produced a profile
            # with company_id="" while still reporting found=True.
            company_resolved = False
            warnings.append(
                f"company {company_hs_id} read failed ({company.status_code}); "
                "company fields are unpopulated"
            )

    decision_maker_raw = contact_props.get("decision_maker")
    decision_maker_flag = (
        "Yes" if str(decision_maker_raw).strip().lower() in {"true", "yes"} else "No"
    )

    profile = LeadProfile(
        employee_id=contact_props.get("employee_id") or (employee_id or ""),
        company_id=company_props.get("company_id") or "",
        decision_maker_flag=decision_maker_flag,
        job_title=clean_optional(contact_props.get("jobtitle")),
        email=clean_optional(contact_props.get("email_id")),
        phone=clean_optional(contact_props.get("phone")),
        industry=clean_optional(company_props.get("industry")),
        annual_revenue_m=clean_optional(company_props.get("annualrevenue")),
        frequency_of_purchase=clean_optional(company_props.get("frequency_of_purchase")),
    )

    obs.process.emit(
        "lead_lookup_found",
        lookup_property=lookup_property,
        contact_hs_id=contact_hs_id,
        company_hs_id=company_hs_id,
        company_resolved=company_resolved,
        warnings=warnings or None,
    )
    return LeadProfileRecord(
        profile=profile,
        contact_hs_id=contact_hs_id,
        company_hs_id=company_hs_id,
        found=True,
        company_resolved=company_resolved,
        warnings=warnings,
    )
