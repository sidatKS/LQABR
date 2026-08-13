"""STEP 3 — decision-maker-only list in memory; unresolved flagged, never dropped."""

from __future__ import annotations

from build_profile import build_profiles
from load_csv import EMPLOYEES_FILE, load_csvs


def _build(seed_dir):
    return build_profiles(load_csvs(seed_dir))


def test_only_decision_makers_are_kept(seed_dir):
    result = _build(seed_dir)
    assert result.rows_seen == 5
    assert result.decision_makers_seen == 4  # E5 is "No"
    assert {p.employee_id for p in result.profiles} == {"E1", "E2", "E3"}


def test_decision_maker_flag_match_is_case_insensitive(seed_dir):
    result = _build(seed_dir)
    # E2 is "yes", E3 is "YES" — both qualify
    assert {p.employee_id for p in result.profiles} >= {"E2", "E3"}


def test_unresolved_is_flagged_with_a_reason_and_not_dropped(seed_dir):
    result = _build(seed_dir)
    assert len(result.unresolved) == 1
    unresolved = result.unresolved[0]
    assert unresolved.employee_id == "E4"
    assert unresolved.reason == "bad-data: missing contact/company record"
    assert unresolved.source_row["missing"] == ["company"]
    # flagged, not pushed: it is absent from the profile list
    assert "E4" not in {p.employee_id for p in result.profiles}


def test_no_decision_maker_is_silently_lost(seed_dir):
    result = _build(seed_dir)
    assert result.decision_makers_seen == len(result.profiles) + len(result.unresolved)


def test_industry_is_stripped_and_uppercased(seed_dir):
    result = _build(seed_dir)
    by_id = {p.employee_id: p for p in result.profiles}
    assert by_id["E1"].industry == "MANUFACTURING"  # source was " manufacturing "


def test_optional_fields_become_none_when_blank(seed_dir):
    result = _build(seed_dir)
    by_id = {p.employee_id: p for p in result.profiles}
    assert by_id["E3"].phone is None
    assert by_id["E3"].email == "lead@example.com"


def test_nine_field_contract_is_intact(seed_dir):
    profile = _build(seed_dir).profiles[0]
    assert list(profile.to_dict().keys()) == [
        "employee_id",
        "company_id",
        "decision_maker_flag",
        "job_title",
        "email",
        "phone",
        "industry",
        "annual_revenue_m",
        "frequency_of_purchase",
    ]


def test_summary_counts(seed_dir):
    result = _build(seed_dir)
    assert result.summary == {
        "rows_seen": 5,
        "decision_makers_seen": 4,
        "profiles_built": 3,
        "unresolved": 1,
        "duplicate_contact_rows": 0,
        "duplicate_company_rows": 0,
        "duplicate_employee_rows": 0,
    }
    assert EMPLOYEES_FILE  # contract file name
