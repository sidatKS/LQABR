# Research Agent

Turns a published post plus a lead's profile into a grounded **`lead_context`**
note on the HubSpot contact — and that write is what raises the second trigger
in the campaign chain.

```
blog_summary written on a Ticket
  └─ HubSpot trigger 1 ─► gateway (R-blog-summary) ─► THIS AGENT :8086
        reads the lead + the post, searches the web, writes lead_context
  └─ HubSpot trigger 2 ─► gateway (R2-lead-context) ─► Email agent :8083
```

## What it does

1. **Reads the lead** — `get_lead_profile(objectId)` → industry, company name,
   job title, and the three ids the write tool requires back.
2. **Reads the post** — `get_blog_summary(blog_published_at)` → the summary the
   Summary Agent wrote, plus its industry.
3. **Researches** — a web search grounded on *that company* and *that industry*,
   focused on the post's themes.
4. **Writes `lead_context`** — one paragraph, citations appended, back through
   the MCP.

## HubSpot access

**The MCP is the only door.** This agent holds no HubSpot token and never calls
`api.hubapi.com`. Every read and every write goes through the HubSpot MCP
container, which owns authentication, schema validation and the audit trail.
`tests/test_standalone.py` fails the build if a direct HubSpot hostname ever
appears in the source.

## Standalone

`packages/research_core/` is this agent's **own** library. It imports nothing
from `packages/lqabr_core`, nothing from `mcp/hubspot`, nothing from another
agent. The only coupling to the platform is a runtime one — the MCP URL — and
that is configuration, not an import.

## Layout

| Path | What |
| --- | --- |
| `packages/research_core/settings.py` | every knob, resolved once (env > config map > default) |
| `packages/research_core/obs.py` | process / audit / system streams, native file log |
| `packages/research_core/types.py` | the dataclasses that cross a boundary |
| `packages/research_core/mcp/` | the MCP client + the HubSpot surface |
| `packages/research_core/search/` | the search contract + the Anthropic provider |
| `src/pipeline.py` | one run: read → read → research → write |
| `src/composer.py` | prompt assembly and note bounding |
| `src/service_app.py` | the HTTP surface |
| `src/prompts/research.md` | the model contract — edit copy here, not in code |
| `config/config.yaml` | the agent config map |

## Run

New machine? Follow `docs/SETUP.md` — clone to first campaign, ~10 minutes.

```bash
pip install -r agents/research/requirements.txt

cd agents/research
./setup_env.sh                 # .env from Secret Manager; writes OFF
                               # ./setup_env.sh --live  when you mean it
set -a && source .env && set +a
python3 -m uvicorn service_app:app --port 8086 --app-dir src
```

Source `.env` FIRST — exporting a variable and then sourcing `.env` overwrites
it. The HubSpot MCP container must be up (`:8080`) or every run fails at the
first read.

Headless, one lead:

```bash
python3 agents/research/src/agent.py \
  --object-id 533963448020 --summary-object-id 330008697562 --dry-run
```

Tests (offline, no credentials) — run from the agent directory, not the repo
root: from the root, pytest takes the root as rootdir and tries to collect the
whole repo.

```bash
cd agents/research
PYTHONPATH=src:packages python -m pytest -q
```

## Configuration

`config/config.yaml` is the config map; every value is overridable by a
`LQABR_RESEARCH_*` environment variable. Full table in `docs/ENV_VARS.md`.

Logs: `logs/research/` — three files written by the agent itself
(`research_process.log`, `research_audit.log`, `research_system.log`), joined
on `run_id`; directory from the config map.

## Docs

- `docs/DESIGN.md` — why each piece exists, and the decisions behind it
- `docs/API.md` — the HTTP contract
- `docs/ENV_VARS.md` — every knob
- `docs/SETUP.md` — a new machine, from clone to first campaign
- `docs/RUNBOOK.md` — start it, verify it, read the failures
