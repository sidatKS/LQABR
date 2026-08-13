"""Tests for agents/enrichment/src/mcp_server.py

Verifies that `build_lead_profiles` is reachable as a real MCP tool call —
not just registered — by driving it through an in-process MCP
ClientSession (mcp.shared.memory), the same client machinery a real MCP
host (orchestrator agent, Claude Desktop/Code, etc.) would use, just
without a network hop.
"""

import csv
import json
import sys
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_server import server  # noqa: E402


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
        ],
        ["Employee_ID", "Company_ID", "Job_Title", "Decision_Maker_Flag"],
    )
    _write_csv(
        contacts,
        [
            {"Employee_ID": "E1", "Company_ID": "C1", "Job_Title": "Marketing Specialist", "Email": "e1@x.com", "Phone": "111"},
        ],
        ["Employee_ID", "Company_ID", "Job_Title", "Email", "Phone"],
    )
    _write_csv(
        companies,
        [
            {"Company_ID": "C1", "Industry": "Healthcare", "Annual_Revenue (M₺)": "10.5", "Frequency_of_Purchase": "Monthly"},
        ],
        ["Company_ID", "Industry", "Annual_Revenue (M₺)", "Frequency_of_Purchase"],
    )
    return employees, contacts, companies


@pytest.mark.anyio
async def test_build_lead_profiles_listed_as_mcp_tool():
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        tools = (await client.list_tools()).tools
        names = {t.name for t in tools}
        assert "build_lead_profiles" in names


@pytest.mark.anyio
async def test_call_tool_over_mcp_returns_field_contract(seed_files):
    employees, contacts, companies = seed_files

    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.call_tool(
            "build_lead_profiles",
            {
                "employees_csv": str(employees),
                "contacts_csv": str(contacts),
                "companies_csv": str(companies),
            },
        )

    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["summary"]["profiles_built"] == 1
    profile = payload["profiles"][0]
    assert profile["employee_id"] == "E1"
    assert profile["email"] == "e1@x.com"
    assert profile["industry"] == "Healthcare"


@pytest.mark.anyio
async def test_call_tool_over_mcp_flags_unresolved_not_dropped(tmp_path):
    employees = tmp_path / "employees.csv"
    contacts = tmp_path / "contacts.csv"
    companies = tmp_path / "companies.csv"
    _write_csv(
        employees,
        [{"Employee_ID": "E9", "Company_ID": "C9", "Job_Title": "Buyer", "Decision_Maker_Flag": "Yes"}],
        ["Employee_ID", "Company_ID", "Job_Title", "Decision_Maker_Flag"],
    )
    _write_csv(contacts, [], ["Employee_ID", "Company_ID", "Job_Title", "Email", "Phone"])
    _write_csv(companies, [], ["Company_ID", "Industry", "Annual_Revenue (M₺)", "Frequency_of_Purchase"])

    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.call_tool(
            "build_lead_profiles",
            {
                "employees_csv": str(employees),
                "contacts_csv": str(contacts),
                "companies_csv": str(companies),
            },
        )

    payload = json.loads(result.content[0].text)
    assert payload["profiles"] == []
    assert payload["unresolved"][0]["employee_id"] == "E9"
    assert "bad-data" in payload["unresolved"][0]["reason"]
