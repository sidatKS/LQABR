"""Lead-profile building — raw source records -> 9-pointer LeadProfile -> HubSpot.

This is the deterministic core of the Lead Profile Agent. It lives in
lqabr_core (not in the agent) so the ingestion trigger, the ADK agent and
tests can all use the same logic without cross-agent imports.

Input contract — a "raw lead record" is a plain dict with these normalized
keys (either ingestion source produces them; see agents/ingestion):

    full_name, job_title, company, email, phone, industry,
    company_size, annual_revenue, location, linkedin_url,
    external_employee_id, external_company_id, source ("csv"|"zoominfo")

Rules (CLAUDE.md): bad/messy records are flagged and returned as
`unresolved` with an explicit reason — never crash the run, never silently
drop a lead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from lqabr_core.crm.base import CRMClient, CRMError
from lqabr_core.probability import BASE_PROBABILITY
from lqabr_core.types import LeadProfile, LeadSource, LeadStage


@dataclass(frozen=True)
class UnresolvedRecord:
    """A record that couldn't become a workable lead — flagged, not dropped."""

    external_employee_id: Optional[str]
    external_company_id: Optional[str]
    reason: str          # e.g. "bad-data: no email and no phone"
    raw: Dict[str, Any] = field(hash=False, default_factory=dict)


@dataclass
class ProfileRunResult:
    profiles: List[LeadProfile] = field(default_factory=list)
    unresolved: List[UnresolvedRecord] = field(default_factory=list)
    upsert_failures: List[UnresolvedRecord] = field(default_factory=list)

    @property
    def summary(self) -> Dict[str, int]:
        return {
            "records_seen": len(self.profiles) + len(self.unresolved) + len(self.upsert_failures),
            "profiles_built": len(self.profiles),
            "unresolved": len(self.unresolved),
            "upsert_failures": len(self.upsert_failures),
        }


def _company_size_revenue(record: Dict[str, Any]) -> Optional[str]:
    size = (record.get("company_size") or "").strip()
    revenue = (record.get("annual_revenue") or "").strip()
    if size and revenue:
        return f"{size} / {revenue}"
    return size or revenue or None


def build_profile(record: Dict[str, Any]) -> LeadProfile:
    """Map one normalized raw record to a 9-pointer LeadProfile (no I/O)."""
    return LeadProfile(
        full_name=(record.get("full_name") or "").strip() or None,
        job_title=(record.get("job_title") or "").strip() or None,
        company=(record.get("company") or "").strip() or None,
        email=(record.get("email") or "").strip() or None,
        phone=(record.get("phone") or "").strip() or None,
        industry=(record.get("industry") or "").strip() or None,
        company_size_revenue=_company_size_revenue(record),
        location=(record.get("location") or "").strip() or None,
        linkedin_url=(record.get("linkedin_url") or "").strip() or None,
        timezone=(record.get("timezone") or "").strip() or None,
        source=LeadSource(record.get("source") or "csv"),
        external_employee_id=record.get("external_employee_id"),
        external_company_id=record.get("external_company_id"),
        stage=LeadStage.PROFILED,
        probability=BASE_PROBABILITY,
    )


def profile_records(records: List[Dict[str, Any]]) -> ProfileRunResult:
    """Build profiles for all records; non-contactable records go to unresolved."""
    result = ProfileRunResult()
    for record in records:
        profile = build_profile(record)
        if not profile.is_contactable:
            result.unresolved.append(
                UnresolvedRecord(
                    external_employee_id=profile.external_employee_id,
                    external_company_id=profile.external_company_id,
                    reason="bad-data: no email and no phone",
                    raw=record,
                )
            )
            continue
        result.profiles.append(profile)
    return result


def profile_and_upsert(records: List[Dict[str, Any]], crm: CRMClient) -> ProfileRunResult:
    """Full Lead Profile Agent run: build 9-pointer profiles and upsert each
    to HubSpot. CRM failures are collected per-record (flagged, not dropped,
    and never abort the whole batch)."""
    result = profile_records(records)
    upserted: List[LeadProfile] = []
    for profile in result.profiles:
        try:
            upserted.append(crm.upsert_lead(profile))
        except CRMError as exc:
            result.upsert_failures.append(
                UnresolvedRecord(
                    external_employee_id=profile.external_employee_id,
                    external_company_id=profile.external_company_id,
                    reason=f"crm-error: {exc}",
                    raw=profile.to_dict(),
                )
            )
    result.profiles = upserted
    return result
