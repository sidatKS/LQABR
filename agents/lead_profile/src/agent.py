"""lead_profile_agent — ONE agentic run, CSVs to HubSpot. NO model by default.

    adk run agents/lead_profile/src         one agentic run in the terminal
    adk web agents                          the same agent in the ADK UI
    lead-profile-agent                      the same agent, headless (Cloud Run Job)

All three drive the same ``root_agent``. There is exactly one orchestrator per
process, chosen here:

    LQABR_ORCHESTRATOR=deterministic   DEFAULT. BaseAgent, zero model calls,
                                       zero API keys, §7.4 intact. The pipeline
                                       (pipeline_agent.py).
    LQABR_ORCHESTRATOR=llm             OPT-IN. The conversational console over
                                       the same tools (llm_agent.py). Needs a
                                       model key; tokens log goes live.

DECIDED 31 Jul (Stephen): the pipeline has no decision points, so it does not
need an LLM — an Anthropic billing outage had just stopped ingestion, which a
model-free default makes impossible. "Agentic" and "LLM" are independent in
ADK; this repo now uses both axes deliberately.

    STEP 1+2  load the three CSVs, halt loudly on structural failure
    STEP 3    join + filter -> list[LeadProfile] in memory
    STEP 4    loop, one in-process MCP upsert per record
                STEP A  get_hubspot_token()    before every HubSpot call
                STEP 5  upsert_lead_profiles() validate + upsert + associate
    STEP 6    get_lead_profile — the read path email / voice / scheduler share
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from google.genai import types

from lqabr_core.obs import close_file_sinks

EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 2
EXIT_PARTIAL_FAILURE = 3

APP_NAME = "lqabr-lead-profile"

HEADLESS_PROMPT = "run the pipeline{scope}{incoming}"


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv

        here = Path(__file__).resolve()
        # ADK reads .env from the agent directory; match that headlessly. This
        # module now lives in <agent>/src/, so the agent dir is its parent —
        # check both, plus the process cwd, never overriding real env vars.
        load_dotenv(here.parent / ".env", override=False)         # <agent>/src/.env
        load_dotenv(here.parent.parent / ".env", override=False)  # <agent>/.env
        load_dotenv(override=False)
    except Exception:
        pass  # prod injects real env vars; .env is a local convenience only


def build_root_agent():
    """Select the orchestrator. Deterministic unless someone explicitly opts in."""
    mode = os.getenv("LQABR_ORCHESTRATOR", "deterministic").strip().lower()
    if mode == "llm":
        try:
            from .llm_agent import build_llm_agent
        except ImportError:  # pragma: no cover
            from llm_agent import build_llm_agent  # type: ignore

        return build_llm_agent()
    if mode != "deterministic":
        raise ValueError(
            f"unknown LQABR_ORCHESTRATOR: {mode!r} (expected 'deterministic' or 'llm')"
        )
    try:
        from .pipeline_agent import DeterministicLeadProfileAgent
    except ImportError:  # pragma: no cover
        from pipeline_agent import DeterministicLeadProfileAgent  # type: ignore

    return DeterministicLeadProfileAgent(name="lead_profile_agent")


# .env must be loaded BEFORE the mode is read — adk imports this module first.
_load_dotenv_if_present()
root_agent = build_root_agent()


# ---------------------------------------------------------------------------
# Headless entrypoint — the SAME agent, no second orchestrator.
# ---------------------------------------------------------------------------


async def run_headless(
    incoming_dir: str | None = None,
    *,
    limit: int | None = None,
    user_id: str = "cloud-run-job",
    runner: object | None = None,
) -> dict[str, object]:
    """Invoke root_agent once, headlessly. Used by the Cloud Run Job entrypoint."""
    if runner is None:
        from google.adk.runners import InMemoryRunner

        runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)

    app_name = getattr(runner, "app_name", APP_NAME)
    session = await runner.session_service.create_session(app_name=app_name, user_id=user_id)
    prompt = HEADLESS_PROMPT.format(
        scope=f" limit {limit}" if limit else "",
        incoming=f" from {incoming_dir}" if incoming_dir else "",
    )

    texts: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.content and event.content.parts:
            texts.extend(part.text for part in event.content.parts if part.text)

    final_session = await runner.session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session.id
    )
    return {
        "summary": texts[-1] if texts else "",
        "transcript": texts,
        "state": dict(final_session.state or {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lead-profile-agent",
        description=(
            "LQABR lead_profile agent — one agentic run: CSVs -> in-memory "
            "LeadProfile list -> HubSpot, via the in-process MCP. Deterministic "
            "by default (no model); same agent as `adk run agents/lead_profile/src`."
        ),
    )
    parser.add_argument("--incoming-dir", default=None, help="directory holding the three CSVs")
    parser.add_argument("--limit", type=int, default=None, help="push only the first N profiles")
    args = parser.parse_args(argv)

    try:
        outcome = asyncio.run(run_headless(args.incoming_dir, limit=args.limit))
    finally:
        close_file_sinks()

    print("\n\n".join(outcome["transcript"]))
    state = outcome["state"] or {}
    if state.get("feed_loaded") is False:
        return EXIT_STRUCTURAL_FAILURE
    if state.get("failed"):
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
