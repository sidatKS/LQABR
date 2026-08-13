"""Regression tests for the 31-Jul code review findings.

One test per finding, named after it, so a future change that reintroduces the
bug fails with the bug's name rather than a puzzle.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import requests

from build_profile import build_profiles
from load_csv import RawFeed
from lqabr_core.obs import Observability, RunContext, get_obs, set_current_run, set_obs
from lqabr_core.leadgen.hubspot import auth as auth_module
from lqabr_core.leadgen.hubspot.crm import get_lead_profile, upsert_lead_profiles
from lqabr_core.leadgen.hubspot.schema import LeadProfile


def make(**overrides) -> LeadProfile:
    base = dict(
        employee_id="E1",
        company_id="C1",
        decision_maker_flag="Yes",
        job_title="Head of Ops",
        email="lead@example.com",
        phone="555-0001",
        industry="MANUFACTURING",
        annual_revenue_m="12.5",
        frequency_of_purchase="Quarterly",
    )
    base.update(overrides)
    return LeadProfile(**base)


def _feed(contacts=None, companies=None, employees=None) -> RawFeed:
    return RawFeed(
        employees=employees
        or [{"Employee_ID": "E1", "Company_ID": "C1", "Job_Title": "X", "Decision_Maker_Flag": "Yes"}],
        contacts=contacts
        or [{"Employee_ID": "E1", "Company_ID": "C1", "Job_Title": "A", "Email": "a@x.com", "Phone": "1"}],
        companies=companies
        or [{"Company_ID": "C1", "Industry": "Retail", "Annual_Revenue (M)": "1", "Frequency_of_Purchase": "M"}],
    )


def _lines(name: str) -> list[dict]:
    path = Path(os.environ["LQABR_ERRORS_DIR"]) / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- B3: 401 forces a token refresh and retries once -----------------------


def test_B3_a_401_forces_a_token_refresh_and_retries(fake_hubspot, fake_response_cls):
    provider = auth_module._CACHE.provider
    real_request = fake_hubspot.request
    state = {"first": True}

    def expire_once(method, url, **kwargs):
        if state["first"] and "/companies/search" in url:
            state["first"] = False
            FakeResponse = fake_response_cls

            return FakeResponse(401, {"message": "expired authentication"})
        return real_request(method, url, **kwargs)

    fake_hubspot.request = expire_once
    before = provider.calls
    result = upsert_lead_profiles(make(), "lead-401")

    assert result.status == "pushed", "an expired token must not burn the lead"
    assert provider.calls > before, "the 401 must force a fresh mint"


# --- B6: the company is written first, and partial state is recorded -------


def test_B6_a_rejected_write_leaves_no_orphan_contact(fake_hubspot):
    fake_hubspot.reject_company_400 = True
    result = upsert_lead_profiles(make(), "lead-partial")

    assert result.status == "failed"
    assert fake_hubspot.contacts == {}, "the Contact must not exist without its Company"

    entry = _lines("schema_mismatch.jsonl")[0]
    assert entry["stage_reached"] == "company_upsert"


def test_B6_a_partial_write_is_visible_in_the_error_record(fake_hubspot):
    fake_hubspot.reject_contact_400 = True
    result = upsert_lead_profiles(make(), "lead-partial")

    assert result.status == "failed"
    entry = _lines("schema_mismatch.jsonl")[0]
    assert entry["stage_reached"] == "contact_upsert"
    assert entry["company_hs_id"] == result.company_hs_id, "what WAS written is on the record"


# --- B7: duplicate source rows are reported, never silently dropped --------


def test_B7_duplicate_contact_rows_are_reported():
    feed = _feed(
        contacts=[
            {"Employee_ID": "E1", "Company_ID": "C1", "Job_Title": "A", "Email": "a@x.com", "Phone": "1"},
            {"Employee_ID": "E1", "Company_ID": "C1", "Job_Title": "B", "Email": "b@x.com", "Phone": "2"},
        ]
    )
    result = build_profiles(feed)
    assert result.duplicate_contact_rows == 1
    assert result.profiles[0].email == "a@x.com", "first row wins, deterministically"


def test_B7_duplicate_employee_rows_are_not_pushed_twice():
    row = {"Employee_ID": "E1", "Company_ID": "C1", "Job_Title": "X", "Decision_Maker_Flag": "Yes"}
    result = build_profiles(_feed(employees=[row, dict(row)]))
    assert len(result.profiles) == 1
    assert result.duplicate_employee_rows == 1


def test_B7_nothing_is_lost_the_counts_reconcile():
    row = {"Employee_ID": "E1", "Company_ID": "C1", "Job_Title": "X", "Decision_Maker_Flag": "Yes"}
    result = build_profiles(_feed(employees=[row, dict(row)]))
    assert result.decision_makers_seen == (
        len(result.profiles) + len(result.unresolved) + result.duplicate_employee_rows
    )


# --- B8: one source of truth for "is a decision maker" --------------------


def test_B8_the_kept_flag_and_the_pushed_bool_cannot_disagree(monkeypatch):
    # The env var that used to desync these is gone; setting it changes nothing.
    monkeypatch.setenv("LQABR_DECISION_MAKER_VALUE", "Y")
    result = build_profiles(_feed())
    profile = result.profiles[0]
    assert profile.to_contact_properties()["decision_maker"] is True


# --- B9: the read path never reports a half-populated profile as healthy ---


def test_B9_a_failed_company_read_is_surfaced_not_silently_empty(fake_hubspot):
    upsert_lead_profiles(make(), "seed")
    fake_hubspot.companies.clear()  # the company GET now 404s

    record = get_lead_profile(employee_id="E1")
    assert record.found is True
    assert record.company_resolved is False
    assert record.warnings, "a consumer must be told why the company fields are empty"
    assert record.to_dict()["company_resolved"] is False


def test_B9_a_healthy_read_reports_company_resolved(fake_hubspot):
    upsert_lead_profiles(make(), "seed")
    record = get_lead_profile(employee_id="E1")
    assert record.company_resolved is True
    assert record.warnings == []
    assert record.profile.company_id == "C1"


# --- B10: two agents in one process log under their own run ---------------


def test_B10_a_second_agent_in_the_process_does_not_steal_the_first_run_id():
    first = RunContext(agent="lead_profile_agent")
    set_current_run(first)
    set_obs(Observability(first))
    assert get_obs().run_id == first.run_id

    second = RunContext(agent="email_agent")
    set_current_run(second)
    resolved = get_obs()

    assert resolved.run_id == second.run_id
    assert resolved.run_ctx.agent == "email_agent"


# --- B11: auth mode fails closed ------------------------------------------


def test_B11_an_unset_auth_mode_is_an_error_not_a_silent_private_app(monkeypatch):
    monkeypatch.delenv("HUBSPOT_AUTH_MODE", raising=False)
    with pytest.raises(auth_module.AuthConfigError) as exc:
        auth_module.build_provider()
    assert "no default" in str(exc.value).lower()


# --- B12: the shared tool normalises before it validates -------------------


def test_B12_a_raw_industry_from_a_consumer_is_normalised_not_rejected(fake_hubspot):
    result = upsert_lead_profiles(make(industry="  manufacturing "), "lead-raw")
    assert result.status == "pushed"
    assert fake_hubspot.companies[result.company_hs_id]["industry"] == "MANUFACTURING"


# --- B13: duplicate dedup keys in HubSpot are logged -----------------------


def test_B13_duplicate_dedup_keys_are_reported(fake_hubspot, capsys):
    fake_hubspot.contacts["9001"] = {"employee_id": "E1", "jobtitle": "old-1"}
    fake_hubspot.contacts["9002"] = {"employee_id": "E1", "jobtitle": "old-2"}

    upsert_lead_profiles(make(), "lead-dup")

    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    duplicates = [e for e in events if e.get("event") == "duplicate_dedup_key"]
    assert duplicates, "two records sharing a dedup key must not be invisible"
    assert duplicates[0]["hs_ids"] == ["9001", "9002"]


# --- B15 / B16: retry-after honoured, error timestamps are UTC -------------


def test_B15_retry_after_is_honoured_over_the_guessed_backoff(
    fake_hubspot, monkeypatch, fake_response_cls
):
    FakeResponse = fake_response_cls

    slept: list[float] = []
    monkeypatch.setattr("lqabr_core.leadgen.hubspot.crm.time.sleep", slept.append)

    real_request = fake_hubspot.request
    state = {"n": 0}

    def throttle_once(method, url, **kwargs):
        if "/companies/search" in url and state["n"] == 0:
            state["n"] += 1
            response = FakeResponse(429, {"message": "rate limited"})
            response.headers = {"Retry-After": "7"}
            return response
        return real_request(method, url, **kwargs)

    fake_hubspot.request = throttle_once
    assert upsert_lead_profiles(make(), "lead-429").status == "pushed"
    assert 7.0 in slept, "HubSpot's Retry-After beats our guess"


def test_B16_error_file_timestamps_are_utc_iso_like_the_logs(fake_hubspot):
    upsert_lead_profiles(make(employee_id="  "), "lead-ts")
    ts = _lines("schema_mismatch.jsonl")[0]["ts"]
    assert ts.endswith("+00:00"), f"expected UTC ISO, got {ts}"
    assert "." in ts, "millisecond precision, matching every log line"


# --- B20: no shared mutable not-found singleton ---------------------------


def test_B20_not_found_is_a_fresh_object_each_time():
    from lqabr_core.leadgen.hubspot.schema import not_found

    first, second = not_found(), not_found()
    assert first is not second
    first.warnings.append("mutated")
    assert second.warnings == []
