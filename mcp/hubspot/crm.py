"""All HubSpot REST work for the MCP.

Every hop through this module carries the step-4 bearer on the header and
is recorded on the caller's audit_log (endpoint, direction, method, status
code, which bearer, retries, failures) — never the token value.

Reuse note: the read path delegates to `lqabr_core.crm.HubSpotClient`,
which already owns the confirmed property mapping, the contact<->company
association walk and the retry policy (3 tries, exponential backoff on
429/5xx). Reimplementing it here would fork a tested mapping for no gain.
What HubSpotClient does not know about — selecting a campaign's leads by
`object_id`, and writing the campaign-complete column — is implemented
directly below against the same REST API with the same retry contract.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from lqabr_core.crm import CRMError, HubSpotClient
from lqabr_core.types import LeadProfile

from mcp.hubspot import NullObservability, ObservabilitySink
from mcp.hubspot.auth import RunTokenCache
from mcp.hubspot.schema import (
    ValidatedProfile,
    campaign_complete_property,
    last_modified_email_property,
    object_id_property,
    validate_profile,
    validate_writeback,
)

BASE_URL = "https://api.hubapi.com"
_RETRYABLE = (429, 500, 502, 503, 504)
_SEARCH_PAGE_MAX = 200


class HubSpotCRM:
    """The MCP's HubSpot face. Constructed once per run, holding the run's
    token cache so every hop re-uses the same short-lived bearer."""

    def __init__(self, tokens: Optional[RunTokenCache] = None,
                 obs: Optional[ObservabilitySink] = None,
                 session: Optional[requests.Session] = None,
                 client_factory: Optional[Callable[..., HubSpotClient]] = None,
                 max_retries: int = 3, backoff_seconds: float = 1.0) -> None:
        self._tokens = tokens or RunTokenCache(obs=obs)
        self._obs = obs or NullObservability()
        self._session = session or requests.Session()
        self._client_factory = client_factory or HubSpotClient
        self._max_retries = max_retries
        self._backoff = backoff_seconds

    # --------------------------------------------------------------- helpers
    def _client(self) -> HubSpotClient:
        """A HubSpotClient bound to the CURRENT run bearer. Rebuilt per call
        so a refreshed token is picked up without rebuilding the MCP."""
        return self._client_factory(access_token=self._tokens.get(), session=self._session)

    def _request(self, method: str, path: str, *, step: int, **kwargs: Any) -> Dict[str, Any]:
        """One audited HubSpot hop with the same retry contract as
        lqabr_core: 3 tries, exponential backoff on 429/5xx, CRMError on
        final failure. A 401 invalidates the cached bearer so the retry
        re-acquires rather than replaying a dead token."""
        url = f"{BASE_URL}{path}"
        last_error: Optional[str] = None

        for attempt in range(self._max_retries):
            bearer = self._tokens.get()
            headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
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
                self._obs.audit(step=step, direction="outbound", endpoint=path, method=method,
                                status_code=None, bearer=bearer, attempt=attempt + 1,
                                error=last_error)
            else:
                self._obs.audit(step=step, direction="outbound", endpoint=path, method=method,
                                status_code=resp.status_code, bearer=bearer, attempt=attempt + 1)
                if resp.status_code == 401:
                    self._tokens.invalidate()
                    last_error = "HTTP 401: bearer rejected, re-acquiring"
                elif resp.status_code in _RETRYABLE:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                elif resp.status_code >= 400:
                    raise CRMError(
                        f"HubSpot {method} {path} failed: HTTP {resp.status_code}: {resp.text[:500]}")
                else:
                    return resp.json() if resp.text else {}
            time.sleep(self._backoff * (2 ** attempt))

        raise CRMError(
            f"HubSpot {method} {path} failed after {self._max_retries} retries: {last_error}")

    # ------------------------------------------------------------- step 5 read
    def get_lead_profile(self, object_id: str) -> ValidatedProfile:
        """One lead's schema-validated 9-parameter profile."""
        profile = self._client().get_lead(str(object_id))
        validated = validate_profile(profile)
        self._obs.process(step=5, event="schema_validated", object_id=validated.object_id,
                          missing_pointers=validated.missing_pointers)
        return validated

    def leads_for_trigger(self, object_id: str, limit: int = 25) -> List[LeadProfile]:
        """The leads HubSpot chunked under this campaign trigger.

        The design's entry point is an object ID and nothing else: the
        profile payload stays in HubSpot and the agent asks for it back by
        that id. Searches contacts on the object-id property, paginating
        past HubSpot's 200-per-page search cap.

        Falls back to the 'never emailed yet' queue when the account has no
        object-id property (HubSpot answers a filter on an unknown property
        with a 400) — flagged loudly on process_log, never silently."""
        prop = object_id_property()
        body: Dict[str, Any] = {
            "filterGroups": [{"filters": [
                {"propertyName": prop, "operator": "EQ", "value": str(object_id)}]}],
            # firstname/lastname are standard HubSpot contact properties; the
            # email greets the lead by first name. employee_id stays the
            # internal identifier and is never written into the prose.
            "properties": ["firstname", "lastname", "email_id", "jobtitle",
                           "phone", "employee_id", "lqabr_email_status",
                           "probability", prop],
            "limit": min(limit, _SEARCH_PAGE_MAX),
        }

        leads: List[LeadProfile] = []
        after: Optional[str] = None
        try:
            while len(leads) < limit:
                body["limit"] = min(limit - len(leads), _SEARCH_PAGE_MAX)
                if after:
                    body["after"] = int(after)
                result = self._request("POST", "/crm/v3/objects/contacts/search",
                                       step=5, json=body)
                for contact in result.get("results", []):
                    leads.append(_row_to_profile(contact))
                after = result.get("paging", {}).get("next", {}).get("after")
                if not after:
                    break
        except CRMError as exc:
            if "HTTP 400" not in str(exc):
                raise
            # The property name is wrong or absent. This used to fall back to
            # the not-yet-emailed queue automatically, and that was dangerous:
            # a campaign asked to work batch X would instead send REAL EMAIL to
            # up to `limit` arbitrary profiled leads, and still return 200. A
            # misconfiguration must never turn into mail to the wrong people.
            #
            # So it fails by default. The fallback survives behind an explicit
            # opt-in for local exploration against an account that has no
            # object-id property yet.
            self._obs.process(
                step=5, event="object_id_property_unavailable", object_id=str(object_id),
                detail=(f"HubSpot rejected a filter on '{prop}'. Confirm the property "
                        "name against the owning schema and set "
                        "LQABR_HUBSPOT_OBJECT_ID_PROPERTY."),
                error=str(exc)[:300])

            if not _queue_fallback_allowed():
                raise CRMError(
                    f"crm-error: HubSpot has no usable contact property '{prop}' — "
                    f"cannot select the leads for object {object_id}. Set "
                    "LQABR_HUBSPOT_OBJECT_ID_PROPERTY to the confirmed property "
                    "name. Refusing to fall back to the not-yet-emailed queue, "
                    "which would email a different set of leads than the campaign "
                    "asked for (set LQABR_EMAIL_ALLOW_QUEUE_FALLBACK=1 to allow "
                    "that, local use only)."
                ) from exc

            self._obs.process(
                step=5, event="object_id_fallback_used", object_id=str(object_id),
                detail="LQABR_EMAIL_ALLOW_QUEUE_FALLBACK=1 — working the "
                       "not-yet-emailed queue INSTEAD of this campaign's batch")
            from lqabr_core.types import LeadStage
            leads = self._client().leads_in_stage(LeadStage.PROFILED, limit=limit)

        self._obs.process(step=5, event="trigger_batch_loaded",
                          object_id=str(object_id), lead_count=len(leads))
        return leads

    # ---------------------------------------------------------- step 9 write
    def patch_object(self, object_id: str, properties: Dict[str, Any],
                     verify_after_seconds: float = 2.0) -> Dict[str, Any]:
        """Validate against the same schema used for the read, then PATCH.

        This is the single write path: `lqabr_email_status`, `probability`
        and the campaign-complete column all land through here.

        HTTP 200 from HubSpot is NOT proof the property actually persisted,
        and there are two distinct ways it can lie:

        1. The PATCH response itself never echoes the field — a
           write-restricted property rejected inline. Caught immediately
           below by comparing the response body to what was sent.
        2. The PATCH response echoes success correctly, but a HubSpot
           WORKFLOW enrolled on this object/property fires immediately
           after and reverts it — seconds later, after the 200 has already
           gone out. That is invisible to the PATCH response no matter how
           carefully it's checked; the only way to catch it is to look
           again. So this re-reads the object `verify_after_seconds` after
           the write and compares THAT against what was sent too. Pass 0 to
           skip the re-read (tests; anywhere a synchronous delay is
           unwanted)."""
        validated = validate_writeback(properties)
        self._obs.process(step=9, event="writeback_validated", object_id=str(object_id),
                          properties=sorted(validated.keys()))
        result = self._request("PATCH", f"/crm/v3/objects/contacts/{object_id}",
                               step=9, json={"properties": validated})
        self._obs.process(step=9, event="writeback_applied", object_id=str(object_id),
                          written=validated)

        # Check 1 — does the PATCH response itself echo what was sent?
        echoed = (result or {}).get("properties") or {}
        mismatched = {name: {"sent": value, "hubspot_returned": echoed.get(name)}
                      for name, value in validated.items()
                      if str(echoed.get(name, "")) != str(value)}
        if mismatched:
            self._obs.process(
                step=9, event="writeback_verification_failed", object_id=str(object_id),
                mismatched=mismatched,
                detail=("HubSpot returned HTTP 200 but its own response body does not "
                        "reflect one or more written properties — likely a workflow or "
                        "a write-restricted property silently overriding/rejecting the "
                        "value. Check the property's automation/permissions in HubSpot."))
            return result

        # Check 2 — re-read after a short delay. The PATCH response can only
        # ever prove what HubSpot accepted AT THAT INSTANT; a workflow acting
        # on the change afterward is a second write this module never made
        # and never sees unless it looks again.
        if verify_after_seconds > 0:
            time.sleep(verify_after_seconds)
            try:
                refetched = self._request(
                    "GET", f"/crm/v3/objects/contacts/{object_id}", step=9,
                    params={"properties": ",".join(sorted(validated.keys()))})
            except CRMError as exc:
                self._obs.process(step=9, event="writeback_reread_failed",
                                  object_id=str(object_id), error=str(exc))
                return result

            current = (refetched or {}).get("properties") or {}
            reverted = {name: {"we_set": value, "hubspot_now_holds": current.get(name)}
                        for name, value in validated.items()
                        if str(current.get(name, "")) != str(value)}
            if reverted:
                self._obs.process(
                    step=9, event="writeback_reverted_after_success", object_id=str(object_id),
                    reverted=reverted,
                    detail=(f"the PATCH echoed success and {verify_after_seconds}s later a "
                            "re-read shows one or more properties no longer hold what was "
                            "written. HubSpot did not reject the write — something changed "
                            "it back afterward. Check Automation > Workflows on the contacts "
                            "object for anything enrolled on this property, and check for a "
                            "duplicate-contact merge (a merge can replay an older value over "
                            "a newer one)."))
        return result

    def mark_sent(self, object_id: str, verify_after_seconds: float = 2.0) -> Dict[str, Any]:
        """Step 7 run-state mirror on the CRM: PENDING -> SENT.

        Also stamps ``last_modified_email`` so the portal column reflects the
        exact moment the email left Mailgun."""
        props: Dict[str, Any] = {"lqabr_email_status": "SENT"}
        lm_prop = last_modified_email_property()
        if lm_prop:
            props[lm_prop] = int(time.time() * 1000)
        return self.patch_object(object_id, props,
                                 verify_after_seconds=verify_after_seconds)

    def mark_campaign_complete(self, object_id: str, verify_after_seconds: float = 2.0) -> Dict[str, Any]:
        """Step 10's hand-off condition: the single column the voice campaign
        reads. Set when a delivered email is clicked — not by a probability
        threshold."""
        return self.patch_object(object_id, {campaign_complete_property(): True},
                                 verify_after_seconds=verify_after_seconds)


def _queue_fallback_allowed() -> bool:
    """Opt-in only. Off by default because the fallback works a DIFFERENT set
    of leads than the campaign asked for, and the caller cannot tell."""
    return os.environ.get("LQABR_EMAIL_ALLOW_QUEUE_FALLBACK", "").strip().lower() \
        in ("1", "true", "yes", "on")


def _row_to_profile(contact: Dict[str, Any]) -> LeadProfile:
    """Search results carry contact-level properties only (walking the
    company association per lead would be N+1 calls on a list). Company
    fields are filled in by `get_lead_profile` when the lead is worked."""
    props = contact.get("properties", {}) or {}
    full_name = " ".join(
        x for x in (props.get("firstname"), props.get("lastname")) if x) or None
    lead = LeadProfile(
        full_name=full_name,
        job_title=props.get("jobtitle"),
        company=props.get("company"),
        email=props.get("email_id"),
        phone=props.get("phone"),
        external_employee_id=props.get("employee_id"),
        probability=int(float(props["probability"])) if props.get("probability") else 0,
        extra={"email_status": props.get("lqabr_email_status") or "PENDING"},
    )
    # object_id is the alias for the shared LeadProfile.hubspot_contact_id, so
    # the identifier reads as object_id everywhere in the email agent.
    lead.object_id = contact.get("id")
    return lead
