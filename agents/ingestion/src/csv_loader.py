"""Manual CSV ingestion source.

The operator drops the three B2B export CSVs into a folder (default:
data/seeds/b2b/) and triggers ingestion with --source csv. This module joins
them into normalized raw lead records (see lqabr_core.profile for the key
contract), keeping the deterministic decision-maker join that the original
Phase 0 enrichment agent proved out:

    employees CSV  -> Employee_ID, Company_ID, Decision_Maker_Flag, Name
    contacts CSV   -> Name, Job_Title, Email, Phone  (join Employee_ID+Company_ID)
    companies CSV  -> Industry, Company_Size, Annual_Revenue, Region/District
                      (join Company_ID)

Unjoinable rows are returned as unresolved raw records with reason
"bad-data" — flagged, never dropped.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Tuple

ANNUAL_REVENUE_COL = "Annual_Revenue (M₺)"

DEFAULT_EMPLOYEES = "employees_with_company_sample.csv"
DEFAULT_CONTACTS = "employee_contacts_5234.csv"
DEFAULT_COMPANIES = "companies_clean_734.csv"


class CsvSourceError(Exception):
    """Unrecoverable input problem (missing file / required column)."""


def _read_csv(path: Path, required: Tuple[str, ...]) -> List[Dict[str, str]]:
    if not path.exists():
        raise CsvSourceError(f"input file not found: {path}")
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise CsvSourceError(f"{path.name} missing required column(s): {', '.join(missing)}")
        return list(reader)


def load_csv_records(
    folder: Path,
    employees_file: str = DEFAULT_EMPLOYEES,
    contacts_file: str = DEFAULT_CONTACTS,
    companies_file: str = DEFAULT_COMPANIES,
    decision_maker_value: str = "Yes",
) -> List[Dict[str, Any]]:
    """Join the three CSVs into normalized raw lead records (decision-makers only).

    Records that fail a join still come back — with whatever fields resolved
    — so the profile builder can flag them rather than lose them.
    """
    folder = Path(folder)
    employees = _read_csv(folder / employees_file,
                          ("Employee_ID", "Company_ID", "Decision_Maker_Flag"))
    contacts = _read_csv(folder / contacts_file,
                         ("Employee_ID", "Company_ID", "Email", "Phone"))
    companies = _read_csv(folder / companies_file,
                          ("Company_ID", "Industry", "Company_Size", ANNUAL_REVENUE_COL))

    contact_index = {(r["Employee_ID"], r["Company_ID"]): r for r in contacts}
    company_index = {r["Company_ID"]: r for r in companies}

    records: List[Dict[str, Any]] = []
    for row in employees:
        if row.get("Decision_Maker_Flag", "").strip().lower() != decision_maker_value.lower():
            continue
        employee_id, company_id = row["Employee_ID"], row["Company_ID"]
        contact = contact_index.get((employee_id, company_id), {})
        company = company_index.get(company_id, {})

        location = ", ".join(x for x in (company.get("Region"), company.get("District")) if x) or None
        records.append({
            "full_name": contact.get("Name") or row.get("Name"),
            "job_title": contact.get("Job_Title") or row.get("Job_Title"),
            "company": company_id,   # seeds carry no company name; external id stands in
            "email": contact.get("Email"),
            "phone": contact.get("Phone"),
            "industry": company.get("Industry") or row.get("Industry"),
            "company_size": company.get("Company_Size") or row.get("Company_Size"),
            "annual_revenue": (f"{company.get(ANNUAL_REVENUE_COL)} M₺"
                               if company.get(ANNUAL_REVENUE_COL) else None),
            "location": location,
            "linkedin_url": None,    # not present in CSV seeds; ZoomInfo provides it
            "external_employee_id": employee_id,
            "external_company_id": company_id,
            "source": "csv",
        })
    return records
