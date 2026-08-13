import ingestion_agent


def test_unknown_source_returns_error():
    result = ingestion_agent.run_ingestion("ftp")
    assert "unknown source" in result["error"]


def test_csv_dry_run_builds_profiles_without_hubspot(tmp_path):
    (tmp_path / "employees_with_company_sample.csv").write_text(
        "Employee_ID,Company_ID,Name,Job_Title,Decision_Maker_Flag\nE1,C1,Jane Smith,VP,Yes\n",
        encoding="utf-8")
    (tmp_path / "employee_contacts_5234.csv").write_text(
        "Employee_ID,Company_ID,Name,Job_Title,Email,Phone\nE1,C1,Jane Smith,VP,j@x.com,+1555\n",
        encoding="utf-8")
    (tmp_path / "companies_clean_734.csv").write_text(
        "Company_ID,Industry,Company_Size,Annual_Revenue (M₺),Region,District\nC1,Mfg,Medium,17.3,A,B\n",
        encoding="utf-8")

    result = ingestion_agent.run_ingestion("csv", csv_folder=str(tmp_path), dry_run=True)
    assert result["summary"]["profiles_built"] == 1
    assert result["profiles"][0]["email"] == "j@x.com"
    assert result["profiles"][0]["stage"] == "profiled"


def test_missing_csv_folder_is_reported_not_raised(tmp_path):
    result = ingestion_agent.run_ingestion("csv", csv_folder=str(tmp_path), dry_run=True)
    assert "not found" in result["error"]
