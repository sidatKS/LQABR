"""The DEFAULT orchestrator — deterministic, agentic, zero model calls.

This is the suite that proves the 31-Jul decision: the whole CSV -> HubSpot
flow is one agentic run under ADK with NO model in the process. Every test
here would pass on a machine with no API key of any kind.
"""

from __future__ import annotations

import json

import pytest
from google.adk.runners import InMemoryRunner
from google.genai import types

import agent as agent_module
from pipeline_agent import (
    DeterministicLeadProfileAgent,
    parse_command,
)


async def _invoke(text: str, **agent_kwargs) -> tuple[list[str], dict]:
    agent = DeterministicLeadProfileAgent(name="lead_profile_agent", **agent_kwargs)
    runner = InMemoryRunner(agent=agent, app_name="test-det")
    session = await runner.session_service.create_session(app_name="test-det", user_id="u")
    texts: list[str] = []
    async for event in runner.run_async(
        user_id="u",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        if event.content and event.content.parts:
            texts.extend(part.text for part in event.content.parts if part.text)
    final = await runner.session_service.get_session(
        app_name="test-det", user_id="u", session_id=session.id
    )
    return texts, dict(final.state or {})


# --- command parsing: written rules, not vibes ------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("run the pipeline", {"action": "run"}),
        ("Run the pipeline limit 5", {"action": "run", "limit": 5}),
        ("push the first 20", {"action": "run", "limit": 20}),
        ("dry run", {"action": "run", "dry_run": True}),
        ("run the pipeline from /tmp/x", {"action": "run", "incoming_dir": "/tmp/x"}),
        ("lookup E1042", {"action": "lookup", "employee_id": "E1042"}),
        ("lookup email a@b.com", {"action": "lookup", "email": "a@b.com"}),
        ("show E1042", {"action": "lookup", "employee_id": "E1042"}),
        ("", {"action": "help"}),
        ("what is the weather", {"action": "help"}),
        # plain-English selective operations (the written grammar)
        ("push all records to hubspot", {"action": "run"}),
        ("send the manufacturing leads to hubspot", {"action": "run", "industry": "manufacturing"}),
        ("push company C123 and C9", {"action": "run", "company_ids": "C123,C9"}),
        ("push employees E1042, E1077", {"action": "run", "employee_ids": "E1042,E1077"}),
        ("sync leads E1 E3", {"action": "run", "employee_ids": "E1,E3"}),
        ("how many retail leads do we have?", {"action": "count", "industry": "retail"}),
        ("count company C1", {"action": "count", "company_ids": "C1"}),
        (
            "write the first 10 retail leads to hubspot",
            {"action": "run", "limit": 10, "industry": "retail"},
        ),
        ("can you please upload only company C2 records", {"action": "run", "company_ids": "C2"}),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # a verb inside prose or a question must never fire 263 CRM writes
        "how do i run this",
        "what does push do",
        "is it safe to run now?",
        "the pipeline failed yesterday, why?",
    ],
)
def test_prose_containing_a_verb_is_help_not_a_push(text):
    assert parse_command(text)["action"] == "help"


@pytest.mark.parametrize(
    "text,fragment",
    [
        # negation cannot be expressed — refusing beats inverting the intent
        ("push everything except company C1", "exclusions"),
        ("don't push anything", "exclusions"),
        ("push all leads excluding retail", "exclusions"),
        ("run the pipeline but skip E1042", "exclusions"),
        # a named selection with unrecognisable ids must not widen to a full push
        ("push company Acme", "couldn't recognise an id"),
        ("push employees ABC", "couldn't recognise an id"),
        # a bare verb has no scope
        ("push", "no scope"),
        ("run", "no scope"),
        ("sync now", "no scope"),
    ],
)
def test_dangerous_phrasings_are_refused_with_an_explanation(text, fragment):
    command = parse_command(text)
    assert command["action"] == "refuse"
    assert fragment in command["message"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("push companies C1, C2 & C3", "C1,C2,C3"),
        ("push company C1 or C2", "C1,C2"),
    ],
)
def test_id_list_separators(text, expected):
    assert parse_command(text)["company_ids"] == expected


def test_multi_word_industry_and_bare_quantity():
    assert parse_command("push the financial services leads")["industry"] == "financial services"
    assert parse_command("how many oil & gas leads") == {"action": "count", "industry": "oil & gas"}
    assert parse_command("push 50 leads")["limit"] == 50


def test_unrecognised_input_gets_help_never_a_guess():
    # An LLM would improvise here; the written rule refuses to.
    assert parse_command("delete everything in hubspot")["action"] == "help"


# --- the whole flow, one agentic run, no model ------------------------------


@pytest.mark.asyncio
async def test_full_run_csvs_to_hubspot_with_zero_model_calls(seed_dir, fake_hubspot):
    texts, state = await _invoke(f"run the pipeline from {seed_dir}")

    assert state["feed_loaded"] is True
    assert state["profiles_built"] == 3
    assert state["pushed"] == 3
    assert len(fake_hubspot.associations) == 3
    assert "Run complete." in texts[-1]
    assert "No lead was dropped" in texts[-1]


@pytest.mark.asyncio
async def test_limit_smoke_test(seed_dir, fake_hubspot):
    _, state = await _invoke(f"run the pipeline limit 1 from {seed_dir}")
    assert state["pushed"] == 1
    assert len(fake_hubspot.contacts) == 1


@pytest.mark.asyncio
async def test_dry_run_makes_no_hubspot_calls(seed_dir, fake_hubspot):
    texts, state = await _invoke(f"dry run from {seed_dir}")
    assert state["profiles_built"] == 3
    assert fake_hubspot.calls == []
    assert "no token minted" in texts[-1].lower()


@pytest.mark.asyncio
async def test_structural_failure_halts_and_names_the_file(tmp_path, fake_hubspot):
    empty = tmp_path / "empty"
    empty.mkdir()
    texts, state = await _invoke(f"run the pipeline from {empty}")
    assert state["feed_loaded"] is False
    assert "HALTED" in texts[-1]
    assert "employees_with_company_sample.csv" in texts[-1]
    assert fake_hubspot.calls == []


@pytest.mark.asyncio
async def test_systemic_auth_failure_halts_without_per_lead_records(
    seed_dir, fake_hubspot, monkeypatch
):
    import os
    from pathlib import Path

    from lqabr_core.leadgen.hubspot import auth as auth_module

    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")
    auth_module.reset_token_cache()

    texts, state = await _invoke(f"run the pipeline from {seed_dir}")
    assert state.get("halted") is True
    assert "systemic" in texts[-1].lower()
    assert not (Path(os.environ["LQABR_ERRORS_DIR"]) / "schema_mismatch.jsonl").exists()


@pytest.mark.asyncio
async def test_lookup_reads_one_lead(seed_dir, fake_hubspot):
    await _invoke(f"run the pipeline from {seed_dir}")
    texts, _ = await _invoke("lookup E1")
    assert "Found." in texts[-1]
    assert "contact_hs_id=" in texts[-1]


@pytest.mark.asyncio
async def test_lookup_not_found(fake_hubspot):
    texts, _ = await _invoke("lookup E999")
    assert "Not found" in texts[-1]


@pytest.mark.asyncio
async def test_lookup_key_must_look_like_an_id(fake_hubspot):
    """'lookup NOPE' (no digit, no @) is refused with help — never guessed."""
    texts, _ = await _invoke("lookup NOPE")
    assert "run the pipeline" in texts[-1]  # the usage text


@pytest.mark.asyncio
async def test_help_on_empty_message(fake_hubspot):
    texts, _ = await _invoke("")
    assert "run the pipeline" in texts[-1]
    assert "lookup" in texts[-1]


# --- observability: four streams wired, tokens correctly SILENT -------------


@pytest.mark.asyncio
async def test_tokens_log_is_silent_because_no_model_runs(seed_dir, fake_hubspot, capsys):
    await _invoke(f"run the pipeline from {seed_dir}")

    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    streams = {line["log"] for line in lines}
    assert {"system", "process", "audit"} <= streams
    assert "tokens" not in streams, "no model -> the fourth log must be empty (§7.4)"

    assert any(line.get("event") == "operator_command" for line in lines)
    orchestration = [l for l in lines if l.get("event") == "agent_invoked"]
    assert orchestration[0]["orchestration"] == "deterministic"

    # one run_id spans everything, and it is the ADK invocation id
    assert len({line["run_id"] for line in lines}) == 1


@pytest.mark.asyncio
async def test_run_headless_uses_the_deterministic_default(seed_dir, fake_hubspot, monkeypatch):
    monkeypatch.delenv("LQABR_ORCHESTRATOR", raising=False)
    agent = agent_module.build_root_agent()
    assert isinstance(agent, DeterministicLeadProfileAgent)

    runner = InMemoryRunner(agent=agent, app_name="test-headless")
    outcome = await agent_module.run_headless(
        incoming_dir=str(seed_dir), runner=runner, user_id="job"
    )
    assert outcome["state"]["pushed"] == 3
    assert "Run complete." in outcome["summary"]


# --- selective operations, end to end, still zero model calls ---------------


@pytest.mark.asyncio
async def test_push_only_named_company_in_plain_english(seed_dir, fake_hubspot):
    texts, state = await _invoke(f"push company C2 to hubspot from {seed_dir}")
    assert state["pushed"] == 1
    assert len(fake_hubspot.companies) == 1
    assert "Selection: 1 of 3" in "\n".join(texts)


@pytest.mark.asyncio
async def test_push_named_employees_in_plain_english(seed_dir, fake_hubspot):
    _, state = await _invoke(f"sync leads E1 E3 from {seed_dir}")
    assert state["pushed"] == 2
    pushed = {c["employee_id"] for c in fake_hubspot.contacts.values()}
    assert pushed == {"E1", "E3"}


@pytest.mark.asyncio
async def test_push_by_industry_in_plain_english(seed_dir, fake_hubspot):
    _, state = await _invoke(f"send the retail leads to hubspot from {seed_dir}")
    assert state["pushed"] == 1  # only E3/C2 is Retail in the seed


@pytest.mark.asyncio
async def test_zero_match_selection_refuses_and_writes_nothing(seed_dir, fake_hubspot):
    texts, _ = await _invoke(f"push company NOPE9 to hubspot from {seed_dir}")
    assert "matched 0" in texts[-1]
    assert "never widen" in texts[-1]
    assert fake_hubspot.contacts == {}


@pytest.mark.asyncio
async def test_count_in_plain_english_makes_no_hubspot_calls(seed_dir, fake_hubspot):
    texts, _ = await _invoke(f"how many manufacturing leads do we have? from {seed_dir}")
    assert "2 of 3" in texts[-1]
    assert "Nothing was pushed" in texts[-1]
    assert fake_hubspot.calls == []
