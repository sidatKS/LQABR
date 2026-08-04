"""Parser accuracy eval — deterministic (measured anywhere) vs LLM (measured live).

    python evals/run_eval.py            # deterministic parser, no creds needed
    python evals/run_eval.py --llm      # same corpus through the LlmAgent
                                        # (needs Vertex/Gemini ADC; makes
                                        #  NO HubSpot calls — fake CRM)

--llm scores tiers A/B/D objectively from the FIRST tool call the model makes;
tier C transcripts are printed for human judgement (novel English has no single
right answer). Every model tool call is captured; nothing reaches HubSpot.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO / "agents" / "lead_profile" / "src"
AGENT_TESTS = REPO / "agents" / "lead_profile" / "tests"
for _p in (REPO, REPO / "evals", AGENT_SRC, AGENT_TESTS):
    sys.path.insert(0, str(_p))

from corpus import TIERS  # noqa: E402


def score_deterministic() -> None:
    from pipeline_agent import parse_command

    total = exec_ok = safe = miss = danger = 0
    for name, tier in TIERS.items():
        e = s = m = d = 0
        for text, expected in tier:
            got = parse_command(text)
            clean = {k: v for k, v in got.items() if k != "message"}
            if expected == "SAFE":
                if got["action"] == "run":
                    d += 1
                else:
                    s += 1
            elif clean == expected:
                e += 1
            elif got["action"] == "run":
                d += 1
            else:
                m += 1
        n = len(tier)
        total += n; exec_ok += e; safe += s; miss += m; danger += d
        print(f"{name:14} n={n:2}  exec-correct={e:2}  safe-handled={s:2}  "
              f"safe-miss={m}  DANGEROUS={d}")
    print(f"\nTOTAL n={total}: exec-correct={exec_ok}, safe-handled={safe}, "
          f"safe-miss={miss}, DANGEROUS={danger}")


async def score_llm() -> None:
    """Run the corpus through the real LlmAgent; capture tool calls, no CRM."""
    import os
    import tempfile

    os.environ.setdefault("LQABR_SECRET_BACKEND", "env")
    os.environ.setdefault("HUBSPOT_AUTH_MODE", "private_app")
    os.environ.setdefault("HUBSPOT_PRIVATE_APP_TOKEN", "eval-fake-token")

    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from conftest import FakeHubSpot, StubTokenProvider  # agents/lead_profile/tests/conftest.py
    from lqabr_core.leadgen.hubspot import auth as auth_module, crm as crm_module
    from llm_agent import build_llm_agent
    import tools as tools_module

    # seed CSVs in a temp dir; fake HubSpot so nothing real is written
    tmp = Path(tempfile.mkdtemp())
    (tmp / "employees_with_company_sample.csv").write_text(
        "Employee_ID,Company_ID,Job_Title,Decision_Maker_Flag\n"
        "E1,C1,Head of Ops,Yes\nE3,C2,Director,Yes\n")
    (tmp / "employee_contacts_5234.csv").write_text(
        "Employee_ID,Company_ID,Job_Title,Email,Phone\n"
        "E1,C1,Head of Ops,a@x.com,1\nE3,C2,Director,b@x.com,2\n")
    (tmp / "companies_clean_734.csv").write_text(
        "Company_ID,Industry,Annual_Revenue (M),Frequency_of_Purchase\n"
        "C1,Manufacturing,1,Q\nC2,Retail,2,M\n")
    os.environ["LQABR_INCOMING_DIR"] = str(tmp)

    fake = FakeHubSpot()
    auth_module.reset_token_cache(provider=StubTokenProvider())
    crm_module.set_http(crm_module.HubSpotHttp(base_url="https://testserver",
                                               timeout=5, max_retries=1, session=fake))

    calls: list[tuple[str, dict]] = []
    agent = build_llm_agent()
    original_before = agent.before_tool_callback

    def recording_before(tool, args, tool_context):
        calls.append((getattr(tool, "name", str(tool)), dict(args or {})))
        return original_before(tool, args, tool_context) if original_before else None

    agent.before_tool_callback = recording_before
    runner = InMemoryRunner(agent=agent, app_name="parser-eval")

    def expected_to_push_args(expected: dict) -> dict:
        mapping = {"limit": "limit", "employee_ids": "employee_ids",
                   "company_ids": "company_ids", "industry": "industry"}
        return {mapping[k]: v for k, v in expected.items()
                if k in mapping}

    results = {name: {"n": 0, "ok": 0, "danger": 0, "other": 0} for name in TIERS}
    transcripts_c: list[str] = []

    for name, tier in TIERS.items():
        for text, expected in tier:
            calls.clear()
            tools_module.clear_run_store()
            session = await runner.session_service.create_session(
                app_name="parser-eval", user_id="eval")
            final = ""
            try:
                async for ev in runner.run_async(
                    user_id="eval", session_id=session.id,
                    new_message=types.Content(role="user",
                                              parts=[types.Part(text=text)])):
                    if ev.is_final_response() and ev.content and ev.content.parts:
                        final = "".join(p.text or "" for p in ev.content.parts)
            except Exception as exc:  # model/transport error: count as other
                final = f"<error: {exc}>"

            pushes = [c for c in calls if c[0] == "push_lead_profiles"]
            results[name]["n"] += 1

            if name == "C_novel":
                transcripts_c.append(
                    f"\n>>> {text}\n    tools: {calls}\n    reply: {final[:200]}")
                continue

            if expected == "SAFE":
                if pushes:
                    results[name]["danger"] += 1
                    print(f"  !! DANGEROUS  {text!r} -> push{pushes[0][1]}")
                else:
                    results[name]["ok"] += 1
            else:
                want_action = expected["action"]
                if want_action == "run":
                    want = expected_to_push_args(expected)
                    got = {k: v for k, v in (pushes[0][1] if pushes else {}).items()
                           if v not in ("", 0, None)}
                    if pushes and got == want:
                        results[name]["ok"] += 1
                    elif pushes:
                        results[name]["danger"] += 1
                        print(f"  !! WRONG-WRITE {text!r} -> {got}, wanted {want}")
                    else:
                        results[name]["other"] += 1
                        print(f"  -- no push for {text!r}; tools={[c[0] for c in calls]}")
                else:  # count / lookup expectations
                    tool = {"count": "count_lead_profiles",
                            "lookup": "get_lead_profile"}[want_action]
                    if any(c[0] == tool for c in calls) and not pushes:
                        results[name]["ok"] += 1
                    elif pushes:
                        results[name]["danger"] += 1
                        print(f"  !! WRONG-WRITE {text!r} (wanted {want_action})")
                    else:
                        results[name]["other"] += 1

    print("\n=== LLM orchestrator (live model) ===")
    for name, r in results.items():
        if name == "C_novel":
            continue
        print(f"{name:14} n={r['n']:2}  correct={r['ok']:2}  "
              f"other={r['other']}  DANGEROUS={r['danger']}")
    print("\n=== tier C transcripts (human judgement) ===")
    print("\n".join(transcripts_c))
    crm_module.set_http(None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true",
                        help="run the corpus through the live LlmAgent (Gemini/Vertex ADC)")
    args = parser.parse_args()
    if args.llm:
        asyncio.run(score_llm())
    else:
        print("=== deterministic parser (measured) ===")
        score_deterministic()
