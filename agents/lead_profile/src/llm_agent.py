"""The OPT-IN LLM operator console — LQABR_ORCHESTRATOR=llm.

Not the default. DECIDED 31 Jul: the pipeline needs no model (see
pipeline_agent.py). This variant exists for one purpose: a conversational
surface over the SAME five tools, for a human poking at runs in `adk web` —
asking why leads failed, doing ad-hoc lookups, phrasing a smoke test in plain
English. Its failure mode (no API credits) then breaks a chat, not ingestion.

Everything below is unchanged from the reviewed build: data never reaches the
model, preconditions block reordering, halts are terminal, temperature 0,
every tool choice audited, tokens log live.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.genai import types

# Co-located modules; support both package (ADK) and flat (tests) import styles.
try:
    from . import callbacks
    from .tools import PIPELINE_TOOLS
except ImportError:  # pragma: no cover
    import callbacks  # type: ignore
    from tools import PIPELINE_TOOLS  # type: ignore

DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"

INSTRUCTION = """\
You are the LQABR lead_profile agent. You move B2B seed CSVs into HubSpot CRM.

Run the pipeline in this order, one tool call at a time:

  1. load_csv_feed        - read the three CSVs
  2. build_lead_profiles  - join and filter to decision-makers
  3. push_lead_profiles   - upsert leads into HubSpot (all, or a selection)

The operator speaks plain English. Your job is to map their words onto the
tools' STRUCTURED arguments — never onto improvisation:

- "push everything" / "run the pipeline"      -> push_lead_profiles()
- "push only the first 10"                    -> push_lead_profiles(limit=10)
- "push the leads for company C123 and C9"    -> push_lead_profiles(company_ids="C123,C9")
- "push only manufacturing leads"             -> push_lead_profiles(industry="manufacturing")
- "push E1042 and E1077 only"                 -> push_lead_profiles(employee_ids="E1042,E1077")
- "how many retail leads do we have?"         -> count_lead_profiles(industry="retail")
- "what's the current state of E1042?"        -> get_lead_profile(employee_id="E1042")

Selection rules:
- Before a SELECTIVE push, call count_lead_profiles with the same filters and
  tell the operator the matched count in your report.
- Use ONLY ids and industry values the operator actually said. Never invent,
  expand, or guess ids. If the request is ambiguous ("push the important
  ones"), ask the operator to name ids, companies, or an industry — do not pick
  for them.
- If a selection matches 0 profiles, report that and stop. Never widen a
  selection to "everything" on your own.

Rules you must follow:

- Never skip a step and never change the order. Each tool refuses if its
  precondition is not met and tells you what to run first - follow that.
- Never ask for, echo, or invent lead data. The tools return COUNTS. The lead
  records stay in memory by design; you orchestrate, you do not handle them.
- If a tool returns "halt": true, STOP immediately. Do not retry and do not call
  further tools. Report the error verbatim to the operator. A halt means the
  feed is broken or HubSpot/credentials are failing - it is never the fault of
  an individual lead.
- Distinguish the two failure kinds when you report: failed_record means bad
  data in specific leads; failed_transport means HubSpot misbehaved. They need
  different action from the operator, so never merge them into one number.
- If the operator asks for a smoke test, pass a small limit to
  push_lead_profiles (5 is the usual). Otherwise pass 0 for all leads.
- get_lead_profile reads one lead's current HubSpot state. Use it when the
  operator asks about a specific lead; report the returned fields plainly and
  flag company_resolved=false as a warning.

When the run finishes, give the operator a short plain summary: profiles built,
pushed, failed_record, failed_transport, unresolved, and whether the run halted.
Mention errors/schema_mismatch.jsonl, errors/transport_failures.jsonl and
errors/unresolved.jsonl when the corresponding counts are non-zero - no lead is
ever dropped, so every non-pushed lead is on file somewhere.
"""


def _build_model() -> object:
    """LiteLlm (secret-backed, lazy key) for provider/model names; bare string otherwise."""
    name = os.getenv("LQABR_AGENT_MODEL", DEFAULT_MODEL)
    if "/" in name:
        try:
            from .model import SecretBackedLiteLlm
        except ImportError:  # pragma: no cover
            from model import SecretBackedLiteLlm  # type: ignore

        return SecretBackedLiteLlm(model=name)
    return name


def build_llm_agent() -> LlmAgent:
    return LlmAgent(
        name="lead_profile_agent",
        model=_build_model(),
        description=(
            "Deterministic lead-profile pipeline: reads three B2B seed CSVs, builds "
            "decision-maker lead profiles in memory, and upserts each into HubSpot "
            "CRM (Contacts + Companies, associated) through the in-process MCP."
        ),
        instruction=INSTRUCTION,
        tools=list(PIPELINE_TOOLS),
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
        before_agent_callback=callbacks.before_agent,
        after_agent_callback=callbacks.after_agent,
        before_tool_callback=callbacks.before_tool,
        after_tool_callback=callbacks.after_tool,
        after_model_callback=callbacks.after_model,
    )
