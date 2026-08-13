import pytest

from csv_loader import CsvSourceError, load_csv_records

EMPLOYEES = """Employee_ID,Company_ID,Name,Job_Title,Decision_Maker_Flag
E1,C1,Jane Smith,VP Procurement,Yes
E2,C1,Bob Jones,Analyst,No
E3,C2,Sam Lee,Director,Yes
"""
CONTACTS = """Employee_ID,Company_ID,Name,Job_Title,Email,Phone
E1,C1,Jane Smith,VP Procurement,jane@acme.example,+15550001111
"""
COMPANIES = """Company_ID,Industry,Company_Size,Annual_Revenue (M₺),Region,District
C1,Manufacturing,Medium,17.3,Adana,Ceyhan
C2,Software,Small,4.2,Izmir,Konak
"""


@pytest.fixture
def csv_folder(tmp_path):
    (tmp_path / "employees_with_company_sample.csv").write_text(EMPLOYEES, encoding="utf-8")
    (tmp_path / "employee_contacts_5234.csv").write_text(CONTACTS, encoding="utf-8")
    (tmp_path / "companies_clean_734.csv").write_text(COMPANIES, encoding="utf-8")
    return tmp_path


def test_only_decision_makers_are_loaded(csv_folder):
    records = load_csv_records(csv_folder)
    assert {r["external_employee_id"] for r in records} == {"E1", "E3"}


def test_full_join_populates_normalized_keys(csv_folder):
    record = next(r for r in load_csv_records(csv_folder) if r["external_employee_id"] == "E1")
    assert record["full_name"] == "Jane Smith"
    assert record["email"] == "jane@acme.example"
    assert record["industry"] == "Manufacturing"
    assert record["annual_revenue"] == "17.3 M₺"
    assert record["location"] == "Adana, Ceyhan"
    assert record["source"] == "csv"


def test_missing_contact_still_returned_for_flagging(csv_folder):
    record = next(r for r in load_csv_records(csv_folder) if r["external_employee_id"] == "E3")
    assert record["email"] is None and record["phone"] is None  # flagged downstream


def test_missing_file_raises(tmp_path):
    with pytest.raises(CsvSourceError, match="not found"):
        load_csv_records(tmp_path)
