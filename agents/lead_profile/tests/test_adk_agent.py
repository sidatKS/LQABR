"""The OPT-IN LLM orchestrator (LQABR_ORCHESTRATOR=llm).

The DEFAULT orchestrator is deterministic and is tested in
tests/test_deterministic_agent.py. These tests build the LlmAgent variant
explicitly and stub its model, so they assert the *harness*, not judgement:

  * ADK can discover the agent (the thing `adk run` actually needs)
  * every tool declaration is JSON-schema-able
  * one agent invocation carries the run from CSVs to HubSpot
  * lead DATA never leaves the process into the model's context
  * the model cannot skip or reorder a step
  * a halt is honoured and not retried
  * the four logs — including the tokens stream, now that a model runs
"""

from __future__ import annotations

import json
from typing import AsyncGenerator

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools import FunctionTool
from google.genai import types

import agent as agent_module
from llm_agent import build_llm_agent
from tools import PIPELINE_TOOLS, clear_run_store


# ---------------------------------------------------------------------------
# a scripted model: emits a fixed sequence of tool calls, then a summary
# ---------------------------------------------------------------------------


class ScriptedLlm(BaseLlm):
    """Replays a plan of (tool_name, args) then a final text turn.

    Also RECORDS everything the tools returned to it, so a test can assert that
    no lead data ever reached the model's context.
    """

    model: str = "scripted-stub"
    plan: list = []
    seen_tool_results: list = []
    turn: int = 0

    async def generate_content_async(
        self, llm_request, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        for content in llm_request.contents or []:
            for part in content.parts or []:
                if getattr(part, "function_response", None) is not None:
                    self.seen_tool_results.append(part.function_response.response)

        usage = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100 + self.turn,
            candidates_token_count=20,
            total_token_count=120 + self.turn,
        )

        if self.turn < len(self.plan):
            name, args = self.plan[self.turn]
            self.turn += 1
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
                ),
                usage_metadata=usage,
            )
            return

        self.turn += 1
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="run complete")]),
            usage_metadata=usage,
        )


@pytest.fixture
def scripted(monkeypatch):
    """Build the OPT-IN LlmAgent with a scripted stub model."""
    monkeypatch.setenv("LQABR_AGENT_MODEL", "gemini-2.0-flash")

    def _make(plan):
        llm = ScriptedLlm(plan=plan, seen_tool_results=[], turn=0)
        agent = build_llm_agent()
        agent.model = llm
        _AGENT_HOLDER["agent"] = agent
        return llm

    yield _make
    clear_run_store()
    _AGENT_HOLDER.pop("agent", None)


_AGENT_HOLDER: dict = {}


async def _run(plan_llm, capsys=None, *, agent=None) -> dict:
    agent = agent or _AGENT_HOLDER.get("agent")
    runner = InMemoryRunner(agent=agent, app_name="test-lqabr")
    return await agent_module.run_headless(runner=runner, user_id="tester")


# ---------------------------------------------------------------------------
# discovery — what `adk run` / `adk web` actually require
# ---------------------------------------------------------------------------


def test_adk_can_discover_the_agent():
    # adk loads agents/lead_profile/src as a package; src/__init__ re-exports
    # root_agent from the agent module, which tests import by bare name.
    import agent as pkg

    assert hasattr(pkg, "root_agent"), "adk looks for agent.root_agent"
    assert pkg.root_agent.name == "lead_profile_agent"


def test_the_default_orchestrator_is_deterministic():
    """DECIDED 31 Jul: no model in the default path — §7.4 restored."""
    from pipeline_agent import DeterministicLeadProfileAgent

    assert isinstance(agent_module.root_agent, DeterministicLeadProfileAgent)
    assert not hasattr(agent_module.root_agent, "model") or getattr(
        agent_module.root_agent, "model", None
    ) in (None, ""), "the default agent must not carry a model"


def test_the_llm_orchestrator_is_opt_in(monkeypatch):
    monkeypatch.setenv("LQABR_ORCHESTRATOR", "llm")
    monkeypatch.setenv("LQABR_AGENT_MODEL", "gemini-2.0-flash")
    agent = agent_module.build_root_agent()
    from google.adk.agents import LlmAgent

    assert isinstance(agent, LlmAgent)


def test_an_unknown_orchestrator_is_refused(monkeypatch):
    monkeypatch.setenv("LQABR_ORCHESTRATOR", "quantum")
    with pytest.raises(ValueError):
        agent_module.build_root_agent()


def test_every_pipeline_step_is_exposed_as_a_tool():
    names = {getattr(tool, "__name__", None) for tool in PIPELINE_TOOLS}
    assert names == {
        "load_csv_feed",
        "build_lead_profiles",
        "count_lead_profiles",
        "push_lead_profiles",
        "list_unresolved_leads",
        "get_lead_profile",
    }


def test_tool_declarations_are_json_schema_able():
    for tool in PIPELINE_TOOLS:
        declaration = FunctionTool(tool)._get_declaration()
        assert declaration is not None
        assert declaration.description, f"{declaration.name} needs a docstring for the model"
        schema = declaration.parameters_json_schema
        if schema is not None:
            json.dumps(schema)  # must survive the wire
            assert "tool_context" not in (schema.get("properties") or {})


# ---------------------------------------------------------------------------
# one invocation = the whole run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_agent_invocation_runs_csvs_to_hubspot(seed_dir, fake_hubspot, scripted):
    scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"limit": 0}),
        ]
    )
    outcome = await _run(None)

    assert outcome["state"]["feed_loaded"] is True
    assert outcome["state"]["profiles_built"] == 3
    assert outcome["state"]["pushed"] == 3
    assert len(fake_hubspot.associations) == 3
    assert outcome["summary"] == "run complete"


@pytest.mark.asyncio
async def test_lead_data_never_reaches_the_model(seed_dir, fake_hubspot, scripted):
    llm = scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"limit": 0}),
        ]
    )
    await _run(None)

    blob = json.dumps(llm.seen_tool_results, default=str)
    # the seed CSVs' actual lead values
    for leaked in ("lead@example.com", "555-0001", "Head of Ops", "MANUFACTURING"):
        assert leaked not in blob, f"{leaked!r} reached the model's context"
    assert "profiles_built" in blob  # counts DID reach it


@pytest.mark.asyncio
async def test_limit_is_honoured_for_a_smoke_test(seed_dir, fake_hubspot, scripted):
    scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"limit": 1}),
        ]
    )
    outcome = await _run(None)
    assert outcome["state"]["pushed"] == 1
    assert len(fake_hubspot.contacts) == 1


# ---------------------------------------------------------------------------
# guardrails — the model cannot skip, reorder, or half-run a step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pushing_before_building_is_refused_not_half_run(seed_dir, fake_hubspot, scripted):
    llm = scripted([("push_lead_profiles", {"limit": 0})])
    await _run(None)

    refusal = llm.seen_tool_results[0]
    assert refusal["ok"] is False
    assert "next_step" in refusal, "a refusal must tell the model what to run first"
    assert fake_hubspot.calls == [], "nothing may be written to HubSpot"


@pytest.mark.asyncio
async def test_building_before_loading_is_refused(seed_dir, fake_hubspot, scripted):
    llm = scripted([("build_lead_profiles", {})])
    await _run(None)
    assert llm.seen_tool_results[0]["ok"] is False
    assert "load_csv_feed" in llm.seen_tool_results[0]["next_step"]


@pytest.mark.asyncio
async def test_a_structural_feed_failure_halts_and_names_the_file(tmp_path, fake_hubspot, scripted):
    empty = tmp_path / "empty"
    empty.mkdir()
    llm = scripted([("load_csv_feed", {"incoming_dir": str(empty)})])
    outcome = await _run(None)

    result = llm.seen_tool_results[0]
    assert result["ok"] is False
    assert result["halt"] is True
    assert "employees_with_company_sample.csv" in result["error"]
    assert outcome["state"]["feed_loaded"] is False
    assert fake_hubspot.calls == []


@pytest.mark.asyncio
async def test_a_systemic_auth_failure_halts_instead_of_blaming_263_leads(
    seed_dir, fake_hubspot, scripted, monkeypatch
):
    """B1: one missing credential must not become N per-record 'schema mismatches'."""
    from lqabr_core.leadgen.hubspot import auth as auth_module

    llm = scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"limit": 0}),
        ]
    )
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")
    auth_module.reset_token_cache()

    await _run(None)

    push_result = llm.seen_tool_results[-1]
    assert push_result["ok"] is False
    assert push_result["halt"] is True
    assert "systemic" in push_result["error"].lower()

    import os
    from pathlib import Path

    mismatch = Path(os.environ["LQABR_ERRORS_DIR"]) / "schema_mismatch.jsonl"
    assert not mismatch.exists(), "a systemic failure must not write per-lead records"


# ---------------------------------------------------------------------------
# observability — four logs, and the tokens stream is now real
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tokens_log_is_live_now_that_a_model_runs(
    seed_dir, fake_hubspot, scripted, capsys
):
    scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"limit": 0}),
        ]
    )
    await _run(None)

    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    streams = {line["log"] for line in lines}
    assert {"system", "process", "audit", "tokens"} <= streams

    tokens = [line for line in lines if line["log"] == "tokens"]
    assert tokens, "a model ran, so the fourth log must have content"
    assert tokens[0]["input_tokens"] > 0
    assert tokens[0]["output_tokens"] > 0

    # every line shares the one run_id, and it IS the ADK invocation id
    run_ids = {line["run_id"] for line in lines}
    assert len(run_ids) == 1


@pytest.mark.asyncio
async def test_every_tool_choice_is_audited(seed_dir, fake_hubspot, scripted, capsys):
    """The model picks the order, so the order itself must be on the record."""
    scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"limit": 0}),
        ]
    )
    await _run(None)

    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    chosen = [line["tool"] for line in lines if line.get("event") == "tool_call"]
    assert chosen == ["load_csv_feed", "build_lead_profiles", "push_lead_profiles"]
    assert all(line.get("chosen_by") == "model" for line in lines if line.get("event") == "tool_call")


@pytest.mark.asyncio
async def test_unresolved_leads_are_persisted_not_just_counted(seed_dir, fake_hubspot, scripted):
    import os
    from pathlib import Path

    scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
        ]
    )
    await _run(None)

    path = Path(os.environ["LQABR_ERRORS_DIR"]) / "unresolved.jsonl"
    entries = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert len(entries) == 1
    assert entries[0]["employee_id"] == "E4"
    assert entries[0]["reason"].startswith("bad-data")


# ---------------------------------------------------------------------------
# the second front door: the same two tools over the MCP protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_server_exposes_the_same_two_tools():
    from lqabr_core.leadgen.server import server

    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == {"upsert_lead_profile", "get_lead_profile"}
    for tool in tools.values():
        assert tool.description
        json.dumps(tool.input_schema)


@pytest.mark.asyncio
async def test_mcp_server_write_path_is_the_same_implementation(fake_hubspot):
    """The MCP front door must not fork the write logic — one path, two doors."""
    from lqabr_core.leadgen.server import upsert_lead_profile

    result = await upsert_lead_profile(
        employee_id="E1",
        company_id="C1",
        decision_maker_flag="Yes",
        job_title="Head of Ops",
        email="lead@example.com",
        phone="555-0001",
        industry="Manufacturing",   # raw: the tool normalises it (B12)
        annual_revenue_m="12.5",
        frequency_of_purchase="Quarterly",
        lead_ref_id="lead-mcp",
    )

    assert result["status"] == "pushed"
    assert result["lead_ref_id"] == "lead-mcp"
    contact = fake_hubspot.contacts[result["contact_hs_id"]]
    # Standard property (decided 2026-08-25; previously custom email_id).
    assert contact["email"] == "lead@example.com"
    assert "email_id" not in contact
    company = fake_hubspot.companies[result["company_hs_id"]]
    assert company["industry"] == "MANUFACTURING"
    assert (result["contact_hs_id"], result["company_hs_id"]) in fake_hubspot.associations


@pytest.mark.asyncio
async def test_mcp_server_reports_a_systemic_failure_as_systemic(fake_hubspot, monkeypatch):
    from lqabr_core.leadgen.server import upsert_lead_profile
    from lqabr_core.leadgen.hubspot import auth as auth_module

    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")
    auth_module.reset_token_cache()

    result = await upsert_lead_profile(
        employee_id="E1", company_id="C1", decision_maker_flag="Yes"
    )
    assert result["failure_kind"] == "systemic"
    assert result["status"] == "halted"


# ---------------------------------------------------------------------------
# selective read/write: English -> structured filters -> deterministic code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_only_named_employees(seed_dir, fake_hubspot, scripted):
    """'push E1 and E3 only' -> employee_ids filter; nothing else is written."""
    scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"employee_ids": "E1,E3"}),
        ]
    )
    outcome = await _run(None)
    assert outcome["state"]["pushed"] == 2
    pushed_ids = {c["employee_id"] for c in fake_hubspot.contacts.values()}
    assert pushed_ids == {"E1", "E3"}


@pytest.mark.asyncio
async def test_push_only_one_company(seed_dir, fake_hubspot, scripted):
    scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"company_ids": "C2"}),
        ]
    )
    outcome = await _run(None)
    assert outcome["state"]["pushed"] == 1
    assert len(fake_hubspot.companies) == 1


@pytest.mark.asyncio
async def test_push_by_industry_is_case_insensitive(seed_dir, fake_hubspot, scripted):
    scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"industry": "retail"}),
        ]
    )
    outcome = await _run(None)
    assert outcome["state"]["pushed"] == 1  # only E3/C2 is Retail in the seed


@pytest.mark.asyncio
async def test_an_empty_selection_refuses_instead_of_pushing_everything(
    seed_dir, fake_hubspot, scripted
):
    """A 0-match filter must never silently widen to a full push."""
    llm = scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("push_lead_profiles", {"company_ids": "NOPE"}),
        ]
    )
    await _run(None)
    result = llm.seen_tool_results[-1]
    assert result["ok"] is False
    assert "matched 0" in result["error"]
    assert fake_hubspot.contacts == {}, "nothing may be written on a 0-match selection"


@pytest.mark.asyncio
async def test_count_lead_profiles_returns_counts_never_data(seed_dir, fake_hubspot, scripted):
    llm = scripted(
        [
            ("load_csv_feed", {"incoming_dir": str(seed_dir)}),
            ("build_lead_profiles", {}),
            ("count_lead_profiles", {"industry": "manufacturing"}),
        ]
    )
    await _run(None)
    result = llm.seen_tool_results[-1]
    assert result["matched"] == 2 and result["total_built"] == 3
    blob = json.dumps(result)
    for leaked in ("lead@example.com", "555-", "Head of Ops"):
        assert leaked not in blob, "counts only — no lead data to the model"
    assert fake_hubspot.calls == [], "counting is free: no HubSpot call"
