"""STEP 3 — Build the qualified profiles in memory.

End goal: the denormalised, decision-maker-only lead list — the single artifact
the rest of the flow acts on. IN MEMORY ONLY; never written to a file.
(context §7.2: no JSON artifact, no artifacts/ folder, no .json handoff.)

Process:
  1. index contacts by (Employee_ID, Company_ID); index companies by Company_ID
  2. for each employee row where Decision_Maker_Flag.strip().lower() == "yes",
     resolve its contact and its company
  3. both resolve  -> LeadProfile
     either missing -> UnresolvedLead(reason="bad-data: missing contact/company record")

Unresolved rows are flagged and returned SEPARATELY — not pushed to HubSpot and
not discarded (context §7.9).

Logs: system, process. In-memory -> no audit. No model -> no token log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lqabr_core.obs import get_obs
from lqabr_core.leadgen.hubspot.schema import (
    DECISION_MAKER_TRUE,
    LeadProfile,
    UnresolvedLead,
    clean_optional,
    normalise_industry,
)

# Co-located module; support both package (ADK loads src as a package) and flat
# (tests add src/ to sys.path and import by bare name) import styles.
try:
    from .load_csv import RawFeed
except ImportError:  # pragma: no cover
    from load_csv import RawFeed  # type: ignore

# B8: ONE source of truth for "is a decision maker".
#
# The previous build read LQABR_DECISION_MAKER_VALUE here while
# LeadProfile.decision_maker hard-coded "yes" in schema.py. Setting the env var
# to anything else kept the rows AND pushed decision_maker=false for every one
# of them. The FR states the literal, so the literal wins and the env var is
# gone.
DECISION_MAKER_VALUE = DECISION_MAKER_TRUE  # "yes"


@dataclass
class BuildResult:
    """Step 3 output: the list, the flagged rows, and the counts."""

    profiles: list[LeadProfile] = field(default_factory=list)
    unresolved: list[UnresolvedLead] = field(default_factory=list)
    decision_makers_seen: int = 0
    rows_seen: int = 0
    # B7: duplicate source rows used to vanish from the join index with no log
    # line at all — a silent drop, which the contract forbids.
    duplicate_contact_rows: int = 0
    duplicate_company_rows: int = 0
    duplicate_employee_rows: int = 0

    @property
    def summary(self) -> dict[str, int]:
        return {
            "rows_seen": self.rows_seen,
            "decision_makers_seen": self.decision_makers_seen,
            "profiles_built": len(self.profiles),
            "unresolved": len(self.unresolved),
            "duplicate_contact_rows": self.duplicate_contact_rows,
            "duplicate_company_rows": self.duplicate_company_rows,
            "duplicate_employee_rows": self.duplicate_employee_rows,
        }


def _key(row: dict[str, str], *columns: str) -> tuple[str, ...]:
    return tuple((row.get(column) or "").strip() for column in columns)


def build_profiles(feed: RawFeed) -> BuildResult:
    """Join the three feeds into the decision-maker-only LeadProfile list."""
    obs = get_obs()
    target = DECISION_MAKER_VALUE

    obs.process.emit("step3_start", step=3, name="build_profile", decision_maker_value=target)
    obs.system_snapshot("step3_memory_before")

    result = BuildResult(rows_seen=len(feed.employees))

    # 1. indexes — collisions are REPORTED, never silent (B7). First row wins,
    #    so the index is stable regardless of file ordering.
    contacts_by_key: dict[tuple[str, ...], dict[str, str]] = {}
    for row in feed.contacts:
        key = _key(row, "Employee_ID", "Company_ID")
        if key in contacts_by_key:
            result.duplicate_contact_rows += 1
            obs.process.emit(
                "duplicate_source_row",
                source="contacts",
                key={"Employee_ID": key[0], "Company_ID": key[1]},
                resolution="kept the first occurrence, ignored this one",
            )
            continue
        contacts_by_key[key] = row

    companies_by_id: dict[str, dict[str, str]] = {}
    for row in feed.companies:
        company_id = (row.get("Company_ID") or "").strip()
        if company_id in companies_by_id:
            result.duplicate_company_rows += 1
            obs.process.emit(
                "duplicate_source_row",
                source="companies",
                key={"Company_ID": company_id},
                resolution="kept the first occurrence, ignored this one",
            )
            continue
        companies_by_id[company_id] = row

    obs.process.emit(
        "indexes_built",
        contacts_indexed=len(contacts_by_key),
        companies_indexed=len(companies_by_id),
        duplicate_contact_rows=result.duplicate_contact_rows,
        duplicate_company_rows=result.duplicate_company_rows,
    )

    seen_employees: set[tuple[str, str]] = set()

    # 2. filter + join
    for row in feed.employees:
        flag = (row.get("Decision_Maker_Flag") or "").strip().lower()
        if flag != target:
            continue
        result.decision_makers_seen += 1

        employee_id = (row.get("Employee_ID") or "").strip()
        company_id = (row.get("Company_ID") or "").strip()

        # B7: a repeated employee row would otherwise be pushed twice, the
        # second call overwriting the first. Idempotent, but wasteful and
        # invisible.
        if (employee_id, company_id) in seen_employees:
            result.duplicate_employee_rows += 1
            obs.process.emit(
                "duplicate_source_row",
                source="employees",
                key={"Employee_ID": employee_id, "Company_ID": company_id},
                resolution="already built a profile for this lead; not pushing it twice",
            )
            continue
        seen_employees.add((employee_id, company_id))

        contact = contacts_by_key.get((employee_id, company_id))
        company = companies_by_id.get(company_id)

        if contact is None or company is None:
            missing = []
            if contact is None:
                missing.append("contact")
            if company is None:
                missing.append("company")
            unresolved = UnresolvedLead(
                employee_id=employee_id,
                company_id=company_id,
                reason="bad-data: missing contact/company record",
                source_row={
                    "missing": missing,
                    "Employee_ID": employee_id,
                    "Company_ID": company_id,
                    "Job_Title": row.get("Job_Title"),
                    "Decision_Maker_Flag": row.get("Decision_Maker_Flag"),
                },
            )
            result.unresolved.append(unresolved)
            obs.process.emit(
                "lead_unresolved",
                employee_id=employee_id,
                company_id=company_id,
                missing=missing,
                reason=unresolved.reason,
            )
            continue

        # 3. both resolved -> LeadProfile (9 fields, contract order)
        result.profiles.append(
            LeadProfile(
                employee_id=employee_id,
                company_id=company_id,
                decision_maker_flag=(row.get("Decision_Maker_Flag") or "").strip(),
                job_title=clean_optional(contact.get("Job_Title")),
                email=clean_optional(contact.get("Email")),
                phone=clean_optional(contact.get("Phone")),
                industry=normalise_industry(clean_optional(company.get("Industry"))),
                annual_revenue_m=clean_optional(company.get("Annual_Revenue (M)")),
                frequency_of_purchase=clean_optional(company.get("Frequency_of_Purchase")),
            )
        )

    obs.process.emit("step3_complete", step=3, **result.summary)
    obs.system_snapshot("step3_memory_after", profiles_in_memory=len(result.profiles))
    return result
