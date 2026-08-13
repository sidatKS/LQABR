"""STEP 4 — the loop.

The end-to-end run now goes through the ADK agent, so those cases live in
tests/test_adk_agent.py. What remains here is the loop itself: lead_ref_id
assignment, per-lead isolation, and the failure taxonomy that decides whether
one bad lead is a data problem or the dependency is down.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import requests

from container_definition import CONTAINER
from build_profile import build_profiles
from call_mcp import push_profiles
from load_csv import load_csvs
from lqabr_core.leadgen.hubspot.failures import CircuitBreaker


def _profiles(seed_dir):
    return build_profiles(load_csvs(seed_dir)).profiles


def _lines(name: str) -> list[dict]:
    path = Path(os.environ["LQABR_ERRORS_DIR"]) / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_every_record_gets_its_own_lead_ref_id(seed_dir, fake_hubspot):
    summary = push_profiles(_profiles(seed_dir))
    ref_ids = [r.lead_ref_id for r in summary.results]
    assert len(ref_ids) == 3
    assert len(set(ref_ids)) == 3
    assert all(ref.startswith("lead-") for ref in ref_ids)


def test_one_run_id_correlates_the_whole_run(seed_dir, fake_hubspot):
    summary = push_profiles(_profiles(seed_dir))
    assert summary.run_id
    assert summary.summary["profiles_seen"] == 3


def test_summary_counts_pushed_and_failed(seed_dir, fake_hubspot):
    summary = push_profiles(_profiles(seed_dir))
    assert summary.pushed == 3
    assert summary.failed == 0
    assert len(fake_hubspot.associations) == 3


def test_a_single_failure_does_not_stop_the_loop(seed_dir, fake_hubspot):
    profiles = _profiles(seed_dir)
    profiles[1].employee_id = ""  # break the middle one
    summary = push_profiles(profiles)

    assert summary.profiles_seen == 3
    assert summary.pushed == 2
    assert summary.failed == 1
    assert summary.failed_record == 1
    assert summary.failed_transport == 0

    lines = _lines("schema_mismatch.jsonl")
    assert len(lines) == 1
    assert lines[0]["lead_ref_id"] == summary.results[1].lead_ref_id


def test_no_json_artifact_is_written(seed_dir, fake_hubspot):
    push_profiles(_profiles(seed_dir))
    assert [p.name for p in Path(seed_dir).rglob("*.json")] == []
    assert not (Path.cwd() / "artifacts").exists()


# --- B1: a bad record and a dead dependency are not the same thing ---------


def test_a_bad_record_and_a_transport_failure_are_counted_separately(seed_dir, fake_hubspot):
    calls = {"n": 0}
    real_request = fake_hubspot.request

    def flaky(method, url, **kwargs):
        calls["n"] += 1
        if "/companies/search" in url and calls["n"] < 4:
            raise requests.exceptions.ConnectionError("connection reset")
        return real_request(method, url, **kwargs)

    fake_hubspot.request = flaky
    profiles = _profiles(seed_dir)[:1]
    summary = push_profiles(profiles, breaker=CircuitBreaker(limit=0))

    assert summary.failed_transport == 1
    assert summary.failed_record == 0
    assert _lines("transport_failures.jsonl"), "transport failures get their own file"
    assert _lines("schema_mismatch.jsonl") == [], "and never pollute the schema file"


def test_transport_errors_are_retried_before_being_recorded(seed_dir, fake_hubspot):
    """B2: a connection reset used to burn a lead on the first attempt."""
    attempts = {"n": 0}
    real_request = fake_hubspot.request

    def once_flaky(method, url, **kwargs):
        if "/companies/search" in url:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise requests.exceptions.ConnectionError("connection reset")
        return real_request(method, url, **kwargs)

    fake_hubspot.request = once_flaky
    summary = push_profiles(_profiles(seed_dir)[:1])

    assert attempts["n"] >= 2, "the transport exception must be retried"
    assert summary.pushed == 1


def test_consecutive_transport_failures_trip_the_circuit_breaker(seed_dir, fake_hubspot):
    """One outage must not become N misleading per-lead error records."""

    def dead(*args, **kwargs):
        raise requests.exceptions.ConnectionError("hubspot is down")

    fake_hubspot.request = dead
    profiles = _profiles(seed_dir)
    summary = push_profiles(profiles, breaker=CircuitBreaker(limit=2))

    assert summary.halted is True
    assert "circuit breaker" in summary.halt_reason
    assert summary.failed_transport == 2
    assert summary.summary["not_attempted"] == 1
    assert _lines("schema_mismatch.jsonl") == []


def test_a_systemic_auth_failure_halts_and_writes_no_per_lead_record(
    seed_dir, fake_hubspot, monkeypatch
):
    """B1 in one line: a missing env var is not 263 bad leads."""
    from lqabr_core.leadgen.hubspot import auth as auth_module
    from lqabr_core.leadgen.hubspot.failures import SystemicFailure

    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")
    auth_module.reset_token_cache()

    with pytest.raises(SystemicFailure):
        from lqabr_core.leadgen.hubspot.crm import upsert_lead_profiles

        upsert_lead_profiles(_profiles(seed_dir)[0], "lead-systemic")

    assert _lines("schema_mismatch.jsonl") == []
    assert _lines("transport_failures.jsonl") == []


def test_the_loop_stops_on_a_systemic_failure(seed_dir, fake_hubspot, monkeypatch):
    from lqabr_core.leadgen.hubspot import auth as auth_module

    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")
    auth_module.reset_token_cache()

    summary = push_profiles(_profiles(seed_dir))
    assert summary.halted is True
    assert summary.halt_reason.startswith("systemic:")
    assert summary.pushed == 0
    assert summary.failed == 0, "not-attempted leads are not failures"
    assert summary.summary["not_attempted"] == 3


def test_container_declares_the_in_process_mcp():
    dependency = CONTAINER.mcp_dependencies[0]
    assert dependency.in_process is True
    assert dependency.tools == ("upsert_lead_profiles", "get_lead_profile")
    assert "lqabr_core.leadgen.hubspot.auth.get_hubspot_token" in dependency.utilities
    assert CONTAINER.uses_model is False  # deterministic default — §7.4 restored
    assert CONTAINER.orchestrator_default == "deterministic"
