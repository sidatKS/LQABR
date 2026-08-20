"""Step 4 (Rev 5) — Resolve the audience.

New in Rev 5 (D-08): audience resolution moved INTO the gateway. An
``R-blog-summary`` routing decision names a blog-summary Ticket, not a lead.
This module turns that one Ticket into the exact set of leads the post is for:
every contact whose associated Company ``industry`` equals the ticket's
``blog_industry``. One row in, N hand-offs out.

Identifiers only, both ways. The gateway reads *ids* from HubSpot (the ticket's
industry, the company ids, the contact ids) and emits *ids* to the research
agent (each lead's contact id + the ticket id as ``summary_ref_id``). No
lead-profile field is ever read into a trigger or crosses the gateway — the
research agent still resolves each profile itself.

The gateway had never read the CRM before Rev 5; this is that read path, kept
deliberately small — three calls: the ticket, the companies in its industry,
and the contacts at those companies. It runs inside HubSpot's delivery budget,
so a resolution *failure* is a routing error (503 -> HubSpot redelivers), while
a *zero-lead* result is a valid, logged outcome, never an error.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from router import RoutingDecision, RoutingError, RoutingResult


HUBSPOT_BASE = "https://api.hubapi.com"


class AudienceError(RuntimeError):
    """A HubSpot read failed while resolving the audience (a retryable fault)."""


class HubSpotReader:
    """The gateway's small, read-only HubSpot client for audience resolution.

    Three reads and nothing else: the ticket's industry, the companies in that
    industry, and the contacts at those companies. Inject a ``requests.Session``
    to test without a network, matching the house convention elsewhere.
    """

    def __init__(self, token: str, session: Optional[requests.Session] = None,
                 base_url: str = HUBSPOT_BASE, page_size: int = 100,
                 timeout_seconds: int = 8) -> None:
        self._token = token
        self._session = session or requests.Session()
        self._base = base_url.rstrip("/")
        self._page = max(1, min(int(page_size), 100))
        self._timeout = timeout_seconds

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json"}

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            r = self._session.get(self._base + path, headers=self._headers(),
                                  params=params, timeout=self._timeout)
        except requests.RequestException as exc:
            raise AudienceError(f"GET {path} failed: {exc}") from exc
        if r.status_code >= 300:
            raise AudienceError(f"GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            r = self._session.post(self._base + path, headers=self._headers(),
                                   json=body, timeout=self._timeout)
        except requests.RequestException as exc:
            raise AudienceError(f"POST {path} failed: {exc}") from exc
        if r.status_code >= 300:
            raise AudienceError(f"POST {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def ticket_industry(self, ticket_id: str,
                        property_name: str = "blog_industry") -> Optional[str]:
        data = self._get(f"/crm/v3/objects/tickets/{ticket_id}",
                         params={"properties": property_name})
        value = (data.get("properties") or {}).get(property_name)
        return value or None

    def _search_ids(self, object_type: str, filters: List[Dict[str, Any]],
                    properties: List[str], max_results: int) -> List[str]:
        ids: List[str] = []
        after: Optional[str] = None
        while len(ids) < max_results:
            body: Dict[str, Any] = {
                "filterGroups": [{"filters": filters}],
                "properties": properties, "limit": self._page,
            }
            if after:
                body["after"] = after
            data = self._post(f"/crm/v3/objects/{object_type}/search", body)
            for row in data.get("results", []):
                rid = row.get("id")
                if rid:
                    ids.append(str(rid))
            after = (((data.get("paging") or {}).get("next") or {}).get("after"))
            if not after:
                break
        return ids[:max_results]

    def company_ids_for_industry(self, industry: str,
                                 max_results: int = 1000) -> List[str]:
        return self._search_ids(
            "companies",
            [{"propertyName": "industry", "operator": "EQ", "value": industry}],
            ["industry"], max_results)

    def contact_ids_for_companies(self, company_ids: Sequence[str],
                                  max_results: int = 1000) -> List[str]:
        if not company_ids:
            return []
        ids: List[str] = []
        # HubSpot caps an IN filter's value list (~100); chunk to stay safe.
        for i in range(0, len(company_ids), 100):
            if len(ids) >= max_results:
                break
            chunk = list(company_ids[i:i + 100])
            ids.extend(self._search_ids(
                "contacts",
                [{"propertyName": "associatedcompanyid", "operator": "IN",
                  "values": chunk}],
                ["associatedcompanyid"], max_results - len(ids)))
        # A contact could associate to more than one company: de-dup, keep order.
        seen: set = set()
        out: List[str] = []
        for cid in ids:
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out[:max_results]


@dataclass
class AudienceOutcome:
    """What one R-blog resolution produced, for the audit trail (ids/counts only)."""
    trigger_id: str            # the originating R-blog trigger
    summary_ref_id: str        # the ticket id
    industry: Optional[str]
    company_count: int
    lead_count: int


#: Namespace for the per-lead trigger ids minted during expansion. Distinct
#: from the router's trigger namespace and the dispatcher's batch namespace.
_LEAD_NAMESPACE = uuid.UUID("b7e3c1a0-2f8c-5d41-9a6b-0c4a9d5e6f83")


class AudienceResolver:
    """Expands ``R-blog-summary`` decisions into one research hand-off per lead."""

    def __init__(self, reader: HubSpotReader, industry_property: str = "blog_industry",
                 max_audience: int = 1000,
                 blog_route_id: str = "R-blog-summary") -> None:
        self._reader = reader
        self._industry_property = industry_property
        self._max = max(1, int(max_audience))
        self._route_id = blog_route_id

    def _lead_trigger_id(self, summary_ref_id: str, contact_id: str) -> str:
        """Deterministic per (ticket, contact) so a redelivery mints the same id."""
        seed = f"lqabr-lead:{summary_ref_id}:{contact_id}"
        return f"trg-{uuid.uuid5(_LEAD_NAMESPACE, seed).hex[:24]}"

    def needs_resolution(self, decision: RoutingDecision) -> bool:
        return decision.route_id == self._route_id

    def resolve_one(self, decision: RoutingDecision) -> Tuple[List[RoutingDecision],
                                                              AudienceOutcome]:
        """Return (per-lead decisions, outcome). Raises AudienceError on a read fault.

        The R-blog decision's ``object_id`` IS the ticket id, and the ticket id
        is what becomes ``summary_ref_id`` on every lead.
        """
        ticket_id = decision.object_id or ""
        summary_ref_id = ticket_id
        industry = self._reader.ticket_industry(ticket_id, self._industry_property)
        if not industry:
            return [], AudienceOutcome(decision.trigger_id, summary_ref_id, None, 0, 0)
        company_ids = self._reader.company_ids_for_industry(industry, self._max)
        if not company_ids:
            return [], AudienceOutcome(decision.trigger_id, summary_ref_id, industry, 0, 0)
        contact_ids = self._reader.contact_ids_for_companies(company_ids, self._max)
        leads: List[RoutingDecision] = []
        for cid in contact_ids:
            leads.append(replace(
                decision,
                trigger_id=self._lead_trigger_id(summary_ref_id, cid),
                object_id=cid,                      # the CONTACT to fetch
                summary_ref_id=summary_ref_id,      # the blog Ticket
                property_name=self._industry_property,
                property_value=industry,
            ))
        return leads, AudienceOutcome(decision.trigger_id, summary_ref_id,
                                      industry, len(company_ids), len(leads))

    def expand(self, result: RoutingResult, run_id: str,
               audit: Any = None) -> RoutingResult:
        """Replace every R-blog-summary decision with its per-lead decisions.

        Non-blog decisions pass through untouched. A HubSpot read failure
        becomes a routing error (so HubSpot retries the ticket); a zero-lead
        result is recorded and dropped — a valid, non-error outcome.
        """
        new_decisions: List[RoutingDecision] = []
        for decision in result.decisions:
            if not self.needs_resolution(decision):
                new_decisions.append(decision)
                continue
            try:
                leads, outcome = self.resolve_one(decision)
            except AudienceError as exc:
                err = RoutingError(
                    f"audience resolution failed: {exc}",
                    event_id=decision.event_id, object_id=decision.object_id,
                    agent=decision.agent, route_id=decision.route_id,
                    property_name=decision.property_name,
                    property_value=decision.property_value,
                    trigger_id=decision.trigger_id)
                result.errors.append(err)
                _safe_audit(audit, run_id, decision, error=str(exc))
                continue
            new_decisions.extend(leads)
            _safe_audit(audit, run_id, decision, outcome=outcome)
        result.decisions = new_decisions
        return result

    @classmethod
    def from_config(cls, config: Any, token: str,
                    session: Optional[requests.Session] = None) -> "AudienceResolver":
        reader = HubSpotReader(
            token=token, session=session,
            base_url=str(config.get("audience.hubspot_base", HUBSPOT_BASE)),
            page_size=int(config.get("audience.page_size", 100)),
            timeout_seconds=int(config.get("audience.timeout_seconds", 8)))
        return cls(
            reader,
            industry_property=str(config.get("audience.industry_property", "blog_industry")),
            max_audience=int(config.get("audience.max_audience", 1000)),
            blog_route_id=str(config.get("audience.blog_route_id", "R-blog-summary")))


def _safe_audit(audit: Any, run_id: str, decision: RoutingDecision,
                outcome: Optional[AudienceOutcome] = None,
                error: Optional[str] = None) -> None:
    """Auditing must never break resolution — skip cleanly if unavailable."""
    fn = getattr(audit, "record_audience", None)
    if fn is None:
        return
    try:
        fn(run_id, decision, outcome=outcome, error=error)
    except Exception:  # noqa: BLE001
        pass
