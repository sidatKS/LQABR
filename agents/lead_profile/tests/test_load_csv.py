"""STEP 2 — read three CSVs, fail loudly on structural error."""

from __future__ import annotations

import pytest

from load_csv import (
    COMPANIES_FILE,
    CONTACTS_FILE,
    EMPLOYEES_FILE,
    CsvStructureError,
    load_csvs,
)


def test_loads_three_files_into_memory(seed_dir):
    feed = load_csvs(seed_dir)
    assert feed.counts == {"employees": 5, "contacts": 5, "companies": 2}


def test_missing_file_halts_the_run(seed_dir):
    (seed_dir / COMPANIES_FILE).unlink()
    with pytest.raises(CsvStructureError) as exc:
        load_csvs(seed_dir)
    assert COMPANIES_FILE in str(exc.value)


def test_missing_column_names_the_exact_file_and_column(seed_dir):
    (seed_dir / EMPLOYEES_FILE).write_text(
        "Employee_ID,Company_ID,Job_Title\nE1,C1,Head of Ops\n", encoding="utf-8"
    )
    with pytest.raises(CsvStructureError) as exc:
        load_csvs(seed_dir)
    message = str(exc.value)
    assert EMPLOYEES_FILE in message
    assert "Decision_Maker_Flag" in message


def test_revenue_column_name_with_space_and_parens_is_required(seed_dir):
    (seed_dir / COMPANIES_FILE).write_text(
        "Company_ID,Industry,Annual_Revenue,Frequency_of_Purchase\nC1,X,1,Y\n", encoding="utf-8"
    )
    with pytest.raises(CsvStructureError) as exc:
        load_csvs(seed_dir)
    assert "Annual_Revenue (M)" in str(exc.value)


def test_paths_are_overridable_per_file(seed_dir, tmp_path):
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    (elsewhere / "companies.csv").write_text(
        (seed_dir / COMPANIES_FILE).read_text(encoding="utf-8"), encoding="utf-8"
    )
    feed = load_csvs(seed_dir, companies_path=elsewhere / "companies.csv")
    assert len(feed.companies) == 2
    assert len(feed.contacts) == 5
    assert CONTACTS_FILE  # imported name is part of the contract


def test_a_unit_suffixed_column_is_aliased_to_the_canonical_name(seed_dir):
    """The real feed ships 'Annual_Revenue (₹)' where the contract says '(M)'."""
    companies = seed_dir / COMPANIES_FILE
    companies.write_text(
        "Company_ID,Industry,Annual_Revenue (₹),Frequency_of_Purchase\n"
        "C1,Retail,12.5,Quarterly\n",
        encoding="utf-8",
    )
    feed = load_csvs(seed_dir)
    assert feed.companies[0]["Annual_Revenue (M)"] == "12.5"
    assert "Annual_Revenue (₹)" not in feed.companies[0]


def test_an_ambiguous_unit_column_halts_instead_of_guessing(seed_dir):
    companies = seed_dir / COMPANIES_FILE
    companies.write_text(
        "Company_ID,Industry,Annual_Revenue (₹),Annual_Revenue (K₹),Frequency_of_Purchase\n"
        "C1,Retail,12.5,12500,Quarterly\n",
        encoding="utf-8",
    )
    with pytest.raises(CsvStructureError) as exc:
        load_csvs(seed_dir)
    assert "ambiguous" in str(exc.value)


def test_a_truly_missing_unit_column_still_halts(seed_dir):
    companies = seed_dir / COMPANIES_FILE
    companies.write_text(
        "Company_ID,Industry,Frequency_of_Purchase\nC1,Retail,Quarterly\n",
        encoding="utf-8",
    )
    with pytest.raises(CsvStructureError) as exc:
        load_csvs(seed_dir)
    assert "Annual_Revenue (M)" in str(exc.value)


def test_a_bom_and_crlf_feed_loads_cleanly(seed_dir):
    """The real exports are Windows CSVs: CRLF endings, sometimes a BOM."""
    employees = seed_dir / EMPLOYEES_FILE
    employees.write_bytes(
        "﻿Employee_ID,Company_ID,Job_Title,Decision_Maker_Flag\r\n"
        "E1,C1,Head of Ops,Yes\r\n".encode("utf-8")
    )
    feed = load_csvs(seed_dir)
    assert feed.employees[0]["Employee_ID"] == "E1"
