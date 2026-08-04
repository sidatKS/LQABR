# The agentic surface — `adk run` / `adk web`

Rev 5, 31 Jul 2026. Supersedes the CLI-only entrypoint.

## What changed and why

The Rev 4 build was a correct pipeline with an argparse entrypoint. It was not
an agent: there was no `root_agent`, no `google-adk` dependency, and nothing
`adk run` or `adk web` could discover. The requirement is a **single agentic
run** — CSVs to HubSpot, one invocation.

That re-opens project bible §7.4 ("deterministic, NO model"), which was marked
settled. It was re-opened deliberately, and needs sign-off from Mahi / Swaroop.

## Layout ADK needs

```
agents/
  lead_profile/
    .env.example    adk reads .env from the AGENT directory, not the repo root
    requirements.txt
    src/            <- adk imports THIS directory as the agent package
      __init__.py       from .agent import root_agent   <- adk reads root_agent
      agent.py          orchestrator selector + headless CLI (main)
      pipeline_agent.py deterministic BaseAgent — the DEFAULT (no model)
      llm_agent.py      opt-in LlmAgent console (LQABR_ORCHESTRATOR=llm)
      tools.py          the pipeline tools
      callbacks.py      ADK callbacks -> the four logs
      model.py          secret-backed LiteLLM wrapper (llm mode only)
      build_profile.py · call_mcp.py · load_csv.py   the deterministic steps
```

`adk run agents/lead_profile/src` · `adk web agents`

## The three guardrails

An LlmAgent orchestrating 263 CRM writes is only safe because of these. Do not
remove one without replacing it.

### 1. The model never sees lead data

`build_lead_profiles` returns counts. The `RawFeed` and the `list[LeadProfile]`
live in a process-local store keyed by the ADK invocation id
(`tools.py::_RUN_STORE`); ADK session state holds only small JSON markers.

Why not session state directly? It is persisted by the session service and
replayed into the model's context — 263 profiles would be both a context blowup
and a data-handling problem. `test_lead_data_never_reaches_the_model` asserts
that no seed value ever appears in what the model received.

### 2. The model cannot skip or reorder a step

Each tool checks its precondition and returns
`{"ok": false, "error": ..., "next_step": ...}`. `push_lead_profiles` with no
built profiles writes nothing to HubSpot and tells the model what to run first.
Two tests cover the two out-of-order cases.

### 3. A halt is a halt

`{"halt": true}` means the feed is broken, or HubSpot / the credentials are
failing. The instruction says stop and report verbatim; the tools stop producing
work regardless of what the model does next. Combined with idempotent upserts, a
model that retries anyway cannot duplicate a record.

## Observability under ADK

| callback | stream | what it writes |
| --- | --- | --- |
| `before_agent` | system, process | binds `RunContext(run_id=<ADK invocation id>)` — one id across `adk web` and the logs |
| `before_tool` / `after_tool` | process | which tool the model chose, with what args, and what came back |
| `after_model` | **tokens** | input / output / total tokens per model call |
| `after_agent` | system | container_down, and releases the run's in-memory data |

The `tokens` stream was wired-but-silent while this agent had no model. It is
now live, which is what the FR always asked for ("wherever a model runs").

## Cost and failure notes

- Roughly 4–6 model calls per run (three tool calls plus the summary). The work
  is in the 1,300 HubSpot round-trips, not in the model.
- Tools are `async` and offload blocking work with `asyncio.to_thread`, so a
  263-lead push does not stall ADK's event loop or freeze `adk web`.
- `LQABR_AGENT_MODEL` selects the orchestrator. A name containing `/` is routed
  through LiteLLM (`anthropic/claude-sonnet-4-6`, needs `ANTHROPIC_API_KEY`); a
  bare name is native ADK (`gemini-2.0-flash`, needs `GOOGLE_API_KEY`).
- The headless CLI is **not** a second orchestrator: it builds an
  `InMemoryRunner`, sends one fixed prompt to the same `root_agent`, and reads
  the summary back. Cloud Run therefore needs a model key too.

## For the other three agents

`lqabr-mcp-server` exposes the same two tools over MCP. See the header of
`lqabr_core/leadgen/server.py` for the `MCPToolset` snippet. Use that rather than
importing `lqabr_core.leadgen.hubspot.crm` directly, unless you are running in the same
process.
