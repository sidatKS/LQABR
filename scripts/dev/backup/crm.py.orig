"""All HubSpot REST work for the MCP.

Every hop through this module carries the step-4 bearer on the header and
is recorded on the caller's audit_log (endpoint, direction, method, status
code, which bearer, retries, failures) — never the token value.

Reuse note: the read path delegates to `lqabr_core.crm.HubSpotClient`,
which already owns the confirmed contact property mapping and the retry
policy (3 tries, exponential backoff on 429/5xx). Reimplementing it here
would fork a tested mapping for no gain. What HubSpotClient does not know
about — selecting a campaign's leads by `object_id`, walking the
contact<->company association, and writing the campaign-complete column —
is implemented directly below against the same REST API with the same
retry contract.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from lqabr_core.crm import CRMError, HubSpotClient
from lqabr_core.types import LeadProfile

from mcp.hubspot import NullObservability, ObservabilitySink
from mcp.hubspot.auth import RunTokenCache
from mcp.hubspot.schema import (
    ValidatedProfile,
    campaign_complete_property,
    last_modified_email_property,
    lead_context_property,
    object_id_property,
    validate_profile,
    validate_writeback,
)

BASE_URL = "https://api.hubapi.com"
_RETRYABLE = (429, 500, 502, 503, 504)
_SEARCH_PAGE_MAX = 200

#: Company-owned columns. None of these live on the contact, so the step-5
#: contact GET cannot see them however many properties it asks for.
#:
#: WIDENED FOR REV 8 (confirmed live against ldqfingsrv-dev, 2026-08-18).
#: The FRD names what email construction is entitled to — the lead, their
#: company, its industry and the post — and four of those are company-owned
#: and were simply never fetched:
#:
#:     name               the company NAME. Construction had none, so
#:                        DRAFTING_RULES told the model to write around it.
#:     website / domain   the company website URL.
#:     about_us           custom property, "Short about-company". This is the
#:                        FRD's "About-Us description" input.
#:     hs_industry_group  HubSpot's second-tier industry classification. This
#:                        is the FRD's "industry group / sub-domain" input.
#:
#: Every one is populated on the live portal. Asking for a property a portal
#: does not have is harmless on a v3 GET — it comes back absent, and the
#: profile carries the gap rather than failing.
_COMPANY_PROPERTIES = ("name", "website", "domain", "about_us",
                       "industry", "hs_industry_group",
                       "company_id", "annualrevenue", "frequency_of_purchase")

#: Company-backed values that must be present before the association walk can
#: be skipped. `industry` alone is not enough any more.
#:
#: In practice this means the walk NOW RUNS FOR EVERY LEAD that has an
#: associated company, and that is the intended cost. `about_us`,
#: `hs_industry_group` and `website` exist only on the company object — no
#: contact GET can ever carry them, however many properties it asks for — so
#: skipping the walk means drafting without the inputs the FRD names. Two
#: extra GETs per lead is the price of the email being about their business
#: rather than about their job title.
_COMPANY_BACKED_ATTRS = ("industry", "company", "external_company_id")
_COMPANY_BACKED_EXTRAS = ("company_website", "company_about", "industry_group")


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
    def _enrich_from_company(self, profile: LeadProfile) -> None:
        """Fill the company-owned pointers on a contact-shaped profile.

        `industry` is a COMPANY column, not a contact one, and step 6 selects
        the email skill from the industry with no default. So a profile built
        from the contact GET alone always carries `industry=None`, always
        misses the skill, and is always written back FAILED — for every lead,
        regardless of what HubSpot actually holds on the associated company.
        This walks contact -> company and fills the gap before validation, so
        `missing_pointers` reports the real gaps rather than this one.

        Best-effort by design, and mutating in place: a contact with no
        associated company, an association read that fails, or a company read
        that fails all leave the pointers as they were and log a named reason.
        None of them abort the run — the lead is still validated and worked
        with whatever it has, exactly as before this method existed.

        Walks when ANY company-backed value is missing. Rev 7 keyed this on
        `industry` alone, which was safe while industry was the only thing
        taken from the company. Now that the name, website, about-us and
        industry group come from there too — and three of those exist ONLY on
        the company — an industry-carrying contact would have short-circuited
        the walk and silently drafted without them.
        """
        extra = profile.extra or {}
        if (all(getattr(profile, attr, None) for attr in _COMPANY_BACKED_ATTRS)
                and all(extra.get(key) for key in _COMPANY_BACKED_EXTRAS)):
            return

        object_id = str(profile.object_id)
        try:
            assoc = self._request(
                "GET", f"/crm/v4/objects/contacts/{object_id}/associations/companies",
                step=5)
        except CRMError as exc:
            self._obs.process(
                step=5, event="company_association_unavailable", object_id=object_id,
                error=str(exc),
                detail="industry stays empty; the lead falls through skill selection")
            return

        results = (assoc or {}).get("results") or []
        if not results:
            self._obs.process(
                step=5, event="company_association_absent", object_id=object_id,
                detail=("contact is associated with no company — industry, company_id "
                        "and revenue are unavailable for this lead"))
            return

        company_hs_id = str(results[0].get("toObjectId") or results[0].get("id") or "")
        if len(results) > 1:
            self._obs.process(
                step=5, event="company_association_ambiguous", object_id=object_id,
                company_count=len(results), company_hs_id=company_hs_id,
                detail="contact is associated with several companies; using the first")
        if not company_hs_id:
            self._obs.process(
                step=5, event="company_association_absent", object_id=object_id,
                detail="association row carried no company id")
            return

        try:
            company = self._request(
                "GET", f"/crm/v3/objects/companies/{company_hs_id}", step=5,
                params={"properties": ",".join(_COMPANY_PROPERTIES)})
        except CRMError as exc:
            self._obs.process(
                step=5, event="company_read_failed", object_id=object_id,
                company_hs_id=company_hs_id, error=str(exc),
                detail="company fields stay unpopulated; never guessed from the contact")
            return

        props = (company or {}).get("properties") or {}
        filled = []
        for attr, column in (("industry", "industry"),
                             ("company", "name"),
                             ("external_company_id", "company_id"),
                             ("company_size_revenue", "annualrevenue")):
            if not getattr(profile, attr, None) and props.get(column):
                setattr(profile, attr, props[column])
                filled.append(attr)

        # The three that have no `LeadProfile` attribute of their own. They
        # ride on `extra` for the same reason `lead_context` does: LeadProfile
        # is shared with the sibling agents and its 9-pointer shape is not
        # this module's to widen. Rebound rather than mutated — see
        # `_read_lead_context` for why that matters with `replace()`.
        extra = dict(profile.extra or {})
        for key, column in (("company_website", "website"),
                            ("company_about", "about_us"),
                            ("industry_group", "hs_industry_group")):
            value = str(props.get(column) or "").strip()
            if value and not extra.get(key):
                extra[key] = value
                filled.append(key)
        # Fall back to the bare domain when no full website URL is set.
        if not extra.get("company_website") and props.get("domain"):
            extra["company_website"] = str(props["domain"]).strip()
            filled.append("company_website")
        profile.extra = extra

        self._obs.process(
            step=5, event="company_enriched", object_id=object_id,
            company_hs_id=company_hs_id, filled=filled,
            industry=profile.industry, company=profile.company or None,
            detail=("company-owned inputs read off the associated company — "
                    "name, website, about-us and industry group included as of "
                    "rev 8; an empty `filled` means the company holds none"))

    def _read_lead_context(self, profile: LeadProfile) -> None:
        """Read the research agent's knowledge graph onto the profile.

        REV 8 (v4). The research agent persists `lead_context` on the contact
        at step 7 and that write is the hand-off signal that triggers the
        email campaign at step 8; the email agent reads it back here at step 9
        and frames construction with it at step 10. The email agent does no
        research of its own, so if it is not on the record it does not exist.

        `lqabr_core.crm.HubSpotClient` owns the confirmed 9-pointer property
        mapping and knows nothing about this column, and `lqabr_core` is
        shared with the sibling agents — so this reads it with its own audited
        hop rather than forking that mapping, the same way
        `_enrich_from_company` reads the company-owned pointers.

        Best-effort and mutating in place, deliberately. A portal with no such
        property answers a read for it with a 400, and that must not fail a
        run: the lead comes back with an empty context, the email agent's
        step-9 gate flags it with a named reason, and no email goes out on an
        invented narrative. An empty configured property name skips the hop
        entirely."""
        prop = lead_context_property()
        if not prop:
            self._obs.process(
                step=9, event="lead_context_read_disabled",
                object_id=str(profile.object_id),
                detail=("LQABR_HUBSPOT_LEAD_CONTEXT_PROPERTY is empty — not asking "
                        "HubSpot for a research context on this lead"))
            return
        if (profile.extra or {}).get("lead_context"):
            return

        object_id = str(profile.object_id)
        try:
            record = self._request("GET", f"/crm/v3/objects/contacts/{object_id}",
                                   step=9, params={"properties": prop})
        except CRMError as exc:
            self._obs.process(
                step=9, event="lead_context_unavailable", object_id=object_id,
                property_name=prop, error=str(exc)[:300],
                detail=(f"HubSpot would not return '{prop}'. Confirm the property exists "
                        "and set LQABR_HUBSPOT_LEAD_CONTEXT_PROPERTY to its real name "
                        "(or clear it to disable this hop). The lead carries no research "
                        "context and will be flagged, never emailed from a guess."))
            return

        value = str(((record or {}).get("properties") or {}).get(prop) or "").strip()
        # REBOUND, not mutated in place. `LeadProfile` is a dataclass and
        # `dataclasses.replace` copies it SHALLOWLY, so two profiles built from
        # one template share the same `extra` dict — writing into it would leak
        # one lead's research context onto another. Rebinding gives this profile
        # its own dict and leaves any caller's alias untouched.
        profile.extra = {**(profile.extra or {}), "lead_context": value}
        self._obs.process(
            step=9, event="lead_context_read", object_id=object_id,
            property_name=prop, present=bool(value), word_count=len(value.split()),
            detail=("the research agent's knowledge graph for this lead; absent means "
                    "research has not reached it yet, not that the lead is bad"))

    def get_lead_profile(self, object_id: str) -> ValidatedProfile:
        """One lead's schema-validated 9-parameter profile, plus its research
        context.

        The contact GET is only half the profile — the company-owned pointers
        are walked in before validation so `missing_pointers` describes the
        lead, not the fetch — and `lead_context` (rev 8) is read alongside
        them so the email agent's step 9 gets the profile and the knowledge
        graph in one call, as the design specifies."""
        profile = self._client().get_lead(str(object_id))
        self._enrich_from_company(profile)
        self._read_lead_context(profile)
        validated = validate_profile(profile)
        self._obs.process(step=5, event="schema_validated", object_id=validated.object_id,
                          missing_pointers=validated.missing_pointers,
                          has_lead_context=validated.has_lead_context)
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
    # object_id is the shared LeadProfile field (renamed from hubspot_contact_id
    # -> contact_id -> object_id, 2026-08-14), so
    # the identifier reads as object_id everywhere in the email agent.
    lead.object_id = contact.get("id")
    return lead
