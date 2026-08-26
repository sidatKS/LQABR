"""Direct HubSpot REST — the ONE place this agent bypasses the MCP.

Deliberate, scoped, and temporary (2026-08-24, user decision). Every other
CRM read and write in this agent goes through the central MCP, per the repo's
rule. The single exception is "which leads are in this industry", because the
MCP exposes no lead-listing tool: its surface is get/upsert_lead_profile and
get/upsert_blog_summary, all keyed on a single record.

**Delete this module** the moment the MCP grows that tool — point
`LQABR_RESEARCH_MCP_TOOL_LIST_LEADS` at the real name and the campaign path
switches back with no other change.

Why two calls, not one: `industry` lives on the COMPANY, not the contact
(verified live 2026-08-24 — the contact property exists but is empty on every
lead in portal 246777241). So the lookup is:

    companies where industry == X   ->   each company's associated contacts

Read-only. This module never writes to HubSpot; writes stay on the MCP.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

try:
    from .secrets import resolve_secret
    from .settings import Settings, get_settings
except ImportError:  # pragma: no cover - direct import in tests
    from secrets import resolve_secret  # type: ignore
    from settings import Settings, get_settings  # type: ignore


#: The hostname lives here and nowhere else in the agent, so the standalone
#: guard can stay strict about every other file (tests/test_standalone.py).
DEFAULT_BASE_URL = "https://api.hubapi.com"


class HubSpotDirectError(RuntimeError):
    """A direct HubSpot call failed after retries. Never swallowed into a result."""


class HubSpotDirect:
    """Read-only HubSpot REST client for the industry lookup.

    Owns its retry behaviour to match the house contract: 3 tries, exponential
    backoff on 429/5xx and transport errors, every attempt on the audit stream
    with the credential's NAME and the status code — never the token.
    """

    def __init__(self, settings: Optional[Settings] = None, obs: Any = None,
                 session: Optional[requests.Session] = None,
                 token: Optional[str] = None) -> None:
        self._settings = settings or get_settings()
        self._obs = obs
        self._session = session or requests.Session()
        self._token = token
        self._credential_ref = self._settings.hubspot_token_secret

    # ------------------------------------------------------------ transport
    def _bearer(self) -> str:
        if self._token is None:
            self._token = resolve_secret(self._settings.hubspot_token_secret,
                                         settings=self._settings, obs=self._obs)
        return self._token

    def _emit(self, event: str, **fields: Any) -> None:
        if self._obs is not None:
            self._obs.audit.emit(event, **fields)

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 log_params: Optional[Dict[str, Any]] = None,
                 **kwargs: Any) -> Dict[str, Any]:
        """`params` is a real query string and goes to HubSpot; `log_params` is
        what the audit line says. They used to be one argument named `params`
        that was never sent — a reader on the GET below would assume otherwise."""
        url = f"{self._settings.hubspot_base_url or DEFAULT_BASE_URL}{path}"
        sent = {**(params or {}), **(log_params or {})}
        if params:
            kwargs["params"] = params
        headers = {"Authorization": f"Bearer {self._bearer()}",
                   "Content-Type": "application/json"}
        last_error = ""
        for attempt in range(1, self._settings.max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._session.request(
                    method, url, headers=headers,
                    timeout=self._settings.hubspot_timeout_seconds, **kwargs)
            except requests.RequestException as exc:
                last_error = str(exc)
                self._emit("http_out", direction="outbound", service="hubspot",
                           method=method, url=url, attempt=attempt, params=sent,
                           credential=self._credential_ref, error=last_error,
                           duration_ms=(time.perf_counter() - started) * 1000)
            else:
                self._emit("http_out", direction="outbound", service="hubspot",
                           method=method, url=url, attempt=attempt, params=sent,
                           credential=self._credential_ref,
                           status_code=response.status_code,
                           duration_ms=(time.perf_counter() - started) * 1000)
                if response.status_code in self._settings.mcp_retryable_statuses:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                elif response.status_code >= 400:
                    # A 4xx is our request's fault; retrying it just wastes time.
                    raise HubSpotDirectError(
                        f"HubSpot {method} {path} failed: HTTP "
                        f"{response.status_code}: {response.text[:300]}")
                else:
                    return response.json() if response.text else {}
            if attempt < self._settings.max_retries:
                time.sleep(min(self._settings.mcp_backoff_base_seconds * (2 ** (attempt - 1)),
                               self._settings.mcp_backoff_cap_seconds))
        raise HubSpotDirectError(
            f"HubSpot {method} {path} failed after {self._settings.max_retries} "
            f"attempts: {last_error}")

    # --------------------------------------------------------------- lookup
    def _companies_in_industry(self, industry: str, limit: int) -> List[str]:
        """Company record ids whose `industry` matches exactly, paged."""
        ids: List[str] = []
        after: Optional[str] = None
        while len(ids) < limit:
            body: Dict[str, Any] = {
                "filterGroups": [{"filters": [
                    {"propertyName": "industry", "operator": "EQ", "value": industry}]}],
                "properties": ["name", "industry"],
                "limit": min(100, limit - len(ids)),
            }
            if after:
                body["after"] = after
            page = self._request(
                "POST", "/crm/v3/objects/companies/search", json=body,
                # The filter IS the question being asked — a campaign that finds
                # nobody is usually an industry spelled differently over there.
                log_params={"filter": f"industry EQ {industry}",
                            "limit": body["limit"], "after": after or "-",
                            "have": len(ids)})
            ids.extend(str(row["id"]) for row in page.get("results", []) if row.get("id"))
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return ids[:limit]

    def _contacts_of(self, company_id: str) -> List[str]:
        """Every contact on this company — ALL pages of them.

        This used to read page one and stop. A company with more contacts than
        one page lost the remainder with no log line, so the campaign reported
        a `leads_found` that was simply wrong. "Never silently dropped" applies
        to the read side too.
        """
        path = f"/crm/v4/objects/companies/{company_id}/associations/contacts"
        contacts: List[str] = []
        after: Optional[str] = None
        pages = 0
        while True:
            query = {"limit": 100}
            if after:
                query["after"] = after
            page = self._request("GET", path, params=query,
                                 log_params={"company_id": company_id,
                                             "page": pages + 1})
            pages += 1
            contacts.extend(str(row["toObjectId"]) for row in page.get("results", [])
                            if row.get("toObjectId"))
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
            if pages >= 50:      # a company with 5,000 contacts is a data bug
                self._emit("associations_truncated", company_id=company_id,
                           pages=pages, found=len(contacts),
                           reason="stopped after 50 pages — say so rather than "
                                  "return a count that is quietly short")
                break
        return contacts

    def list_leads_by_industry(self, industry: str, limit: int = 100) -> List[str]:
        """Contact record ids for every lead in one industry.

        Raises HubSpotDirectError if the lookup cannot be completed — the
        caller must not mistake a failed search for an empty industry.
        """
        industry = (industry or "").strip()
        if not industry:
            raise HubSpotDirectError("bad-data: no industry to match on")

        companies = self._companies_in_industry(industry, limit)
        if self._obs is not None:
            self._obs.process.emit("industry_companies_found",
                                   industry=industry, count=len(companies),
                                   companies=companies[:10])

        seen: Dict[str, None] = {}     # dedup, insertion-ordered
        for company_id in companies:
            for contact_id in self._contacts_of(company_id):
                seen.setdefault(contact_id, None)
            if len(seen) >= limit:
                break

        contact_ids = list(seen)[:limit]
        return contact_ids
