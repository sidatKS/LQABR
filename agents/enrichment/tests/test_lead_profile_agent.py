"""Tests for agents/enrichment/src/lead_profile_agent.py

lead_profile_agent.py is now a single self-contained module: the pure
join/filter logic (LeadProfileAgent, dataclasses, LeadProfileAgentError)
and the Google ADK wrapper (build_lead_profiles, root_agent) both live
here, with no relative imports between files. Importing it requires
google-adk (it's in agents/enrichment/requirements.txt) since the ADK
Agent is now defined at module import time, not lazily.
"""

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lead_profile_agent import (  # noqa: E402
    ANNUAL_REVENUE_COL,
    LeadProfileAgent,
    LeadProfileAgentError,
    build_lead_profiles,
    root_agent,
    write_outputs,
)


def _write_csv(path: Path, rows, fieldnames):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def seed_files(tmp_path: Path):
    employees = tmp_path / "employees.csv"
    contacts = tmp_path / "contacts.csv"
    companies = tmp_path / "companies.csv"

    _write_csv(
        employees,
        [
            {"Employee_ID": "E1", "Company_ID": "C1", "Job_Title": "Marketing Specialist", "Decision_Maker_Flag": "Yes"},
            {"Employee_ID": "E2", "Company_ID": "C1", "Job_Title": "Analyst", "Decision_Maker_Flag": "No"},
            {"Employee_ID": "E3", "Company_ID": "C2", "Job_Title": "Strategic Buyer", "Decision_Maker_Flag": "yes"},  # case-insensitive
            {"Employee_ID": "E4", "Company_ID": "C3", "Job_Title": "Procurement Lead", "Decision_Maker_Flag": "Yes"},  # missing company
            {"Employee_ID": "E5", "Company_ID": "C1", "Job_Title": "Ops Manager", "Decision_Maker_Flag": "Yes"},  # missing contact
        ],
        ["Employee_ID", "Company_ID", "Job_Title", "Decision_Maker_Flag"],
    )

    _write_csv(
        contacts,
        [
            {"Employee_ID": "E1", "Company_ID": "C1", "Job_Title": "Marketing Specialist", "Email": "e1@x.com", "Phone": "111"},
            {"Employee_ID": "E3", "Company_ID": "C2", "Job_Title": "Strategic Buyer", "Email": "e3@x.com", "Phone": "333"},
        ],
        ["Employee_ID", "Company_ID", "Job_Title", "Email", "Phone"],
    )

    _write_csv(
        companies,
        [
            {"Company_ID": "C1", "Industry": "Healthcare", ANNUAL_REVENUE_COL: "10.5", "Frequency_of_Purchase": "Monthly"},
            {"Company_ID": "C2", "Industry": "Oil & gas", ANNUAL_REVENUE_COL: "20.1", "Frequency_of_Purchase": "Quarterly"},
        ],
        ["Company_ID", "Industry", ANNUAL_REVENUE_COL, "Frequency_of_Purchase"],
    )

    return employees, contacts, companies


def test_filters_to_decision_makers_only(seed_files):
    employees, contacts, companies = seed_files
    result = LeadProfileAgent(employees, contacts, companies).run()
    # E2 (No) must never appear in either bucket
    all_ids = {p.employee_id for p in result.profiles} | {u.employee_id for u in result.unresolved}
    assert "E2" not in all_ids


def test_case_insensitive_decision_maker_flag(seed_files):
    employees, contacts, companies = seed_files
    result = LeadProfileAgent(employees, contacts, companies).run()
    assert any(p.employee_id == "E3" for p in result.profiles)


def test_happy_path_join_fields(seed_files):
    employees, contacts, companies = seed_files
    result = LeadProfileAgent(employees, contacts, companies).run()
    e1 = next(p for p in result.profiles if p.employee_id == "E1")
    assert e1.job_title == "Marketing Specialist"
    assert e1.email == "e1@x.com"
    assert e1.phone == "111"
    assert e1.industry == "Healthcare"
    assert e1.annual_revenue_m == "10.5"
    assert e1.frequency_of_purchase == "Monthly"


def test_missing_company_is_flagged_not_dropped(seed_files):
    employees, contacts, companies = seed_files
    result = LeadProfileAgent(employees, contacts, companies).run()
    unresolved_ids = {u.employee_id: u.reason for u in result.unresolved}
    assert "E4" in unresolved_ids
    assert "company" in unresolved_ids["E4"]


def test_missing_contact_is_flagged_not_dropped(seed_files):
    employees, contacts, companies = seed_files
    result = LeadProfileAgent(employees, contacts, companies).run()
    unresolved_ids = {u.employee_id: u.reason for u in result.unresolved}
    assert "E5" in unresolved_ids
    assert "contact" in unresolved_ids["E5"]


def test_no_lead_silently_dropped(seed_files):
    employees, contacts, companies = seed_files
    result = LeadProfileAgent(employees, contacts, companies).run()
    decision_makers_in_source = 4  # E1, E3, E4, E5
    assert len(result.profiles) + len(result.unresolved) == decision_makers_in_source


def test_missing_file_raises_typed_error(tmp_path):
    with pytest.raises(LeadProfileAgentError):
        LeadProfileAgent(
            tmp_path / "nope.csv", tmp_path / "nope2.csv", tmp_path / "nope3.csv"
        ).run()


def test_missing_required_column_raises_typed_error(tmp_path):
    bad_employees = tmp_path / "employees.csv"
    _write_csv(bad_employees, [{"Employee_ID": "E1"}], ["Employee_ID"])
    contacts = tmp_path / "contacts.csv"
    _write_csv(contacts, [], ["Employee_ID", "Company_ID", "Email", "Phone"])
    companies = tmp_path / "companies.csv"
    _write_csv(companies, [], ["Company_ID", "Industry", ANNUAL_REVENUE_COL, "Frequency_of_Purchase"])

    with pytest.raises(LeadProfileAgentError):
        LeadProfileAgent(bad_employees, contacts, companies).run()


def test_write_outputs_csv_and_json(seed_files, tmp_path):
    employees, contacts, companies = seed_files
    result = LeadProfileAgent(employees, contacts, companies).run()

    out_csv = tmp_path / "out" / "profiles.csv"
    out_json = tmp_path / "out" / "profiles.json"
    write_outputs(result, out_csv, out_json)

    assert out_csv.exists()
    assert out_json.exists()

    with out_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(result.profiles)


# --- ADK wrapper contract tests -------------------------------------------

REQUIRED_PROFILE_FIELDS = (
    "employee_id",
    "company_id",
    "decision_maker_flag",
    "job_title",
    "email",
    "phone",
    "industry",
    "annual_revenue_m",
    "frequency_of_purchase",
)


def test_adk_wrapper_returns_exact_field_contract(seed_files):
    employees, contacts, companies = seed_files

    result = build_lead_profiles(
        employees_csv=str(employees),
        contacts_csv=str(contacts),
        companies_csv=str(companies),
    )

    assert "error" not in result
    assert len(result["profiles"]) > 0
    for profile in result["profiles"]:
        assert set(profile.keys()) == set(REQUIRED_PROFILE_FIELDS)

    e1 = next(p for p in result["profiles"] if p["employee_id"] == "E1")
    assert e1 == {
        "employee_id": "E1",
        "company_id": "C1",
        "decision_maker_flag": "Yes",
        "job_title": "Marketing Specialist",
        "email": "e1@x.com",
        "phone": "111",
        "industry": "Healthcare",
        "annual_revenue_m": "10.5",
        "frequency_of_purchase": "Monthly",
    }


def test_adk_wrapper_root_agent_tool_is_wired():
    assert root_agent.name == "lead_profile_agent"
    assert build_lead_profiles in root_agent.tools
