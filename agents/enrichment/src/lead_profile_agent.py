"""lead_profile_agent.py — Google ADK entry point + join logic for the Lead
Profile (enrichment) agent, all in one module.

Per CLAUDE.md §2 ("Agent engine: Google ADK runtime on Cloud Run, triggered
via Pub/Sub. Pluggable AgentProvider and ModelProvider abstractions"), this
module exposes `root_agent`, the object the ADK CLI/runtime discovers:

    adk web  agents/enrichment/src     # local dev UI
    adk run  agents/enrichment/src     # local dev, terminal
    adk api_server agents/enrichment/src   # what Cloud Run actually serves

This file contains both layers:
  - LeadProfileAgent (+ LeadProfile/UnresolvedLead/EnrichmentResult
    dataclasses, LeadProfileAgentError): the deterministic join/filter
    logic. Typed, mockable, no model or ADK dependency — pass in three CSV
    paths, get back an EnrichmentResult. Per CLAUDE.md §5 ("Tools are
    typed, mockable, and own their own retry/failure behavior").
  - build_lead_profiles + root_agent: the ADK wrapper — a FunctionTool the
    LLM can call, and the Agent definition itself.

Builds a denormalised lead profile for every decision-maker contact by
joining three B2B seed sources:
  1. employees_with_company_sample.csv  -> Employee_ID, Company_ID, Decision_Maker_Flag
  2. employee_contacts_5234.csv         -> Email, Phone   (join on Employee_ID + Company_ID)
  3. companies_clean_734.csv            -> Industry, Annual_Revenue (M₺), Frequency_of_Purchase
                                            (join on Company_ID)

This mirrors the shape of the future BigQuery `fct_lead` model
(data/models/curated/fct_lead.sql) but runs directly against the CSV seeds
so it is usable before the GCP data platform (README "Remaining — needs
you") is provisioned. Once BigLake/BigQuery is live, the CSV reads below can
be swapped for a BigQuery-backed source without touching the join/filter
logic or the tool contract — keeps the enrichment step provider-agnostic,
consistent with the "pluggable provider" convention in CLAUDE.md.

Convention notes (see CLAUDE.md):
  - No canonical state is held here — this agent only *reads* seed/CRM
    exports and produces profiles; writing back to the CRM is out of scope.
  - Never crashes the run or silently drops a lead: unresolved joins are
    flagged with a reason ("bad-data") and returned separately, not skipped.

Field contract: every record in the tool's "profiles" list — and therefore
every lead the agent reports — always has exactly these 9 fields, sourced
from the three-way CSV join: employee_id, company_id, decision_maker_flag,
job_title, email, phone, industry, annual_revenue_m, frequency_of_purchase.
job_title, email, and phone all come from employee_contacts_5234.csv.

Model selection is config-driven (LQABR_ENRICHMENT_MODEL env var) rather than
hard-coded, per §5 ("never add agent- or model-specific logic into the
engine; new agents/models register via config"). Swapping Gemini Flash for
Pro, or a different model entirely, is an env var change — no code edit.

Secrets: GOOGLE_API_KEY / Vertex AI project credentials are read from the
environment. Locally that's `.env` (see .env.example — real values go in a
git-ignored `.env`, never committed); in Cloud Run they come from Secret
Manager per CLAUDE.md §5/§9. This file never reads or hard-codes a key.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from google.adk.agents import Agent

ANNUAL_REVENUE_COL = "Annual_Revenue (M₺)"


@dataclass(frozen=True)
class LeadProfile:
    """One qualified (decision-maker) lead, ready for scoring/outreach handoff."""

    employee_id: str
    company_id: str
    decision_maker_flag: str
    job_title: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    industry: Optional[str]
    annual_revenue_m: Optional[str]
    frequency_of_purchase: Optional[str]


@dataclass(frozen=True)
class UnresolvedLead:
    """A decision-maker row that couldn't be fully enriched — flagged, not dropped."""

    employee_id: str
    company_id: str
    reason: str  # "bad-data": missing contact and/or company record


@dataclass
class EnrichmentResult:
    profiles: List[LeadProfile] = field(default_factory=list)
    unresolved: List[UnresolvedLead] = field(default_factory=list)

    @property
    def summary(self) -> Dict[str, int]:
        return {
            "decision_makers_seen": len(self.profiles) + len(self.unresolved),
            "profiles_built": len(self.profiles),
            "unresolved": len(self.unresolved),
        }


class LeadProfileAgentError(Exception):
    """Raised for unrecoverable input errors (e.g. missing required columns)."""


def _read_csv(path: Path, required_columns: Tuple[str, ...]) -> List[Dict[str, str]]:
    if not path.exists():
        raise LeadProfileAgentError(f"input file not found: {path}")
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in required_columns if c not in (reader.fieldnames or [])]
        if missing:
            raise LeadProfileAgentError(
                f"{path.name} is missing required column(s): {', '.join(missing)}"
            )
        return list(reader)


class LeadProfileAgent:
    """Joins employee/contact/company seed data into decision-maker lead profiles.

    Typed, mockable, side-effect-free (pure read -> transform -> return):
    pass in three CSV paths, get back an EnrichmentResult. No network calls,
    no CRM writes — this agent only builds the profile; handing it off to
    scoring/CRM writeback is a separate tool's responsibility.
    """

    def __init__(
        self,
        employees_csv: Path,
        contacts_csv: Path,
        companies_csv: Path,
        decision_maker_value: str = "Yes",
    ) -> None:
        self.employees_csv = Path(employees_csv)
        self.contacts_csv = Path(contacts_csv)
        self.companies_csv = Path(companies_csv)
        self.decision_maker_value = decision_maker_value

    def run(self) -> EnrichmentResult:
        employees = _read_csv(
            self.employees_csv,
            ("Employee_ID", "Company_ID", "Job_Title", "Decision_Maker_Flag"),
        )
        contacts = _read_csv(
            self.contacts_csv,
            ("Employee_ID", "Company_ID", "Job_Title", "Email", "Phone"),
        )
        companies = _read_csv(
            self.companies_csv,
            ("Company_ID", "Industry", ANNUAL_REVENUE_COL, "Frequency_of_Purchase"),
        )

        contact_index: Dict[Tuple[str, str], Dict[str, str]] = {
            (row["Employee_ID"], row["Company_ID"]): row for row in contacts
        }
        company_index: Dict[str, Dict[str, str]] = {
            row["Company_ID"]: row for row in companies
        }

        result = EnrichmentResult()

        for row in employees:
            if row.get("Decision_Maker_Flag", "").strip().lower() != self.decision_maker_value.lower():
                continue

            employee_id = row["Employee_ID"]
            company_id = row["Company_ID"]

            contact = contact_index.get((employee_id, company_id))
            company = company_index.get(company_id)

            if contact is None or company is None:
                missing = []
                if contact is None:
                    missing.append("contact")
                if company is None:
                    missing.append("company")
                result.unresolved.append(
                    UnresolvedLead(
                        employee_id=employee_id,
                        company_id=company_id,
                        reason=f"bad-data: missing {'/'.join(missing)} record",
                    )
                )
                continue

            result.profiles.append(
                LeadProfile(
                    employee_id=employee_id,
                    company_id=company_id,
                    decision_maker_flag=row["Decision_Maker_Flag"],
                    job_title=contact.get("Job_Title") or None,
                    email=contact.get("Email") or None,
                    phone=contact.get("Phone") or None,
                    industry=company.get("Industry") or None,
                    annual_revenue_m=company.get(ANNUAL_REVENUE_COL) or None,
                    frequency_of_purchase=company.get("Frequency_of_Purchase") or None,
                )
            )

        return result


def write_outputs(
    result: EnrichmentResult, out_csv: Optional[Path], out_json: Optional[Path]
) -> None:
    if out_csv:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(LeadProfile.__dataclass_fields__.keys())
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for profile in result.profiles:
                writer.writerow(asdict(profile))

    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {
            "summary": result.summary,
            "profiles": [asdict(p) for p in result.profiles],
            "unresolved": [asdict(u) for u in result.unresolved],
        }
        out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# agents/enrichment/src/lead_profile_agent.py -> repo root is 3 levels up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_DIR = _REPO_ROOT / "data" / "seeds" / "b2b"

# Defaults point at the Phase 0 CSV seeds (data/seeds/b2b/). Once the GCP data
# platform is live (README "Remaining — needs you"), these can be swapped for
# a BigQuery-backed source (fct_lead's inputs: dim_contact / dim_company) via
# the employees_csv/contacts_csv/companies_csv overrides below — no change to
# the join logic or the tool contract itself.
DEFAULT_EMPLOYEES_CSV = _SEED_DIR / "employees_with_company_sample.csv"
DEFAULT_CONTACTS_CSV = _SEED_DIR / "employee_contacts_5234.csv"
DEFAULT_COMPANIES_CSV = _SEED_DIR / "companies_clean_734.csv"

# Model selectable via config (CLAUDE.md §2/§5) — not hard-coded.
MODEL = os.environ.get("LQABR_ENRICHMENT_MODEL", "gemini-2.0-flash")


def build_lead_profiles(
    decision_maker_value: str = "Yes",
    employees_csv: Optional[str] = None,
    contacts_csv: Optional[str] = None,
    companies_csv: Optional[str] = None,
) -> Dict[str, Any]:
    """Build enriched lead profiles for decision-maker contacts.

    Joins the employee export (Employee_ID, Company_ID, Decision_Maker_Flag)
    with the contact export (Job_Title, Email, Phone) and the company export
    (Industry, Annual_Revenue, Frequency_of_Purchase) into one denormalised
    profile per qualifying contact. Records that can't be fully joined are
    returned separately as `unresolved` with a reason — never silently
    dropped.

    Args:
        decision_maker_value: Value Decision_Maker_Flag must match
            (case-insensitive) to qualify a contact, e.g. "Yes".
        employees_csv: Optional path override for the employee export.
            Defaults to data/seeds/b2b/employees_with_company_sample.csv.
        contacts_csv: Optional path override for the contact export.
            Defaults to data/seeds/b2b/employee_contacts_5234.csv.
        companies_csv: Optional path override for the company export.
            Defaults to data/seeds/b2b/companies_clean_734.csv.

    Returns:
        A dict with "summary" (counts of profiles built vs. unresolved),
        "profiles" (list of enriched lead records), and "unresolved" (list
        of flagged records, each with a reason such as "bad-data: missing
        contact record"). On an unrecoverable input error (e.g. missing
        file or required column), returns {"error": "<message>"} instead
        of raising, so a bad call never crashes the agent run.

        Every item in "profiles" always has exactly these 9 fields:
        employee_id, company_id, decision_maker_flag, job_title, email,
        phone, industry, annual_revenue_m, frequency_of_purchase. For
        example:
        {
          "employee_id": "E00002", "company_id": "C0123",
          "decision_maker_flag": "Yes", "job_title": "Strategic Buyer",
          "email": "svktekninjas@gmail.com", "phone": "415-238-0082",
          "industry": "Oil & gas", "annual_revenue_m": "9.4",
          "frequency_of_purchase": "Quarterly"
        }
    """
    try:
        agent = LeadProfileAgent(
            employees_csv=Path(employees_csv) if employees_csv else DEFAULT_EMPLOYEES_CSV,
            contacts_csv=Path(contacts_csv) if contacts_csv else DEFAULT_CONTACTS_CSV,
            companies_csv=Path(companies_csv) if companies_csv else DEFAULT_COMPANIES_CSV,
            decision_maker_value=decision_maker_value,
        )
        result = agent.run()
    except LeadProfileAgentError as exc:
        return {"error": str(exc), "summary": {}, "profiles": [], "unresolved": []}

    return {
        "summary": result.summary,
        "profiles": [asdict(p) for p in result.profiles],
        "unresolved": [asdict(u) for u in result.unresolved],
    }


root_agent = Agent(
    name="lead_profile_agent",
    model=MODEL,
    description=(
        "Enrichment agent (agents/enrichment) that builds decision-maker lead "
        "profiles by joining employee, contact, and company records."
    ),
    instruction=(
        "You are the lead profile enrichment agent. When asked to enrich, "
        "profile, or list qualified/decision-maker leads, call "
        "build_lead_profiles. Default decision_maker_value to 'Yes' unless "
        "the user specifies a different flag value or source files.\n\n"
        "Your final response MUST include the full `profiles` list returned "
        "by the tool, rendered as JSON, with every record kept exactly as "
        "returned — same 9 fields, same values, nothing summarized, "
        "reworded, or dropped: employee_id, company_id, decision_maker_flag, "
        "job_title, email, phone, industry, annual_revenue_m, "
        "frequency_of_purchase. "
        "Do not paraphrase field values or invent/omit any of them. If the "
        "result set is very large, you may say so and offer to page or "
        "filter it, but never silently truncate without telling the user.\n\n"
        "After the JSON, report the summary counts (profiles built vs. "
        "unresolved), and always surface unresolved records and their "
        "reasons rather than ignoring them — no lead is ever silently "
        "dropped."
    ),
    tools=[build_lead_profiles],
)


def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(description="Build decision-maker lead profiles.")
    parser.add_argument("--employees", required=True, type=Path)
    parser.add_argument("--contacts", required=True, type=Path)
    parser.add_argument("--companies", required=True, type=Path)
    parser.add_argument("--out-csv", type=Path, default=None)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument(
        "--decision-maker-value",
        default="Yes",
        help="Value in Decision_Maker_Flag that qualifies a row (default: Yes)",
    )
    return parser


def _cli_main(argv: Optional[List[str]] = None) -> int:
    """Standalone CLI for local runs, independent of the ADK runtime/model.

    Example:
        python lead_profile_agent.py \\
            --employees data/seeds/b2b/employees_with_company_sample.csv \\
            --contacts  data/seeds/b2b/employee_contacts_5234.csv \\
            --companies data/seeds/b2b/companies_clean_734.csv \\
            --out-csv   data/seeds/b2b/output/lead_profiles_decision_makers.csv \\
            --out-json  data/seeds/b2b/output/lead_profiles_decision_makers.json
    """
    import sys

    args = _build_arg_parser().parse_args(argv)
    try:
        agent = LeadProfileAgent(
            employees_csv=args.employees,
            contacts_csv=args.contacts,
            companies_csv=args.companies,
            decision_maker_value=args.decision_maker_value,
        )
        result = agent.run()
    except LeadProfileAgentError as exc:
        print(f"lead_profile_agent: {exc}", file=sys.stderr)
        return 1

    write_outputs(result, args.out_csv, args.out_json)

    summary = result.summary
    print(
        f"decision-makers seen: {summary['decision_makers_seen']} | "
        f"profiles built: {summary['profiles_built']} | "
        f"unresolved (flagged, not dropped): {summary['unresolved']}"
    )
    if args.out_csv:
        print(f"wrote CSV  -> {args.out_csv}")
    if args.out_json:
        print(f"wrote JSON -> {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
