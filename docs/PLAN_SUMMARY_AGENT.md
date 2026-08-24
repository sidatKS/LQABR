# Plan — `agents/summary` (standalone ADK Summary Agent)

**Status:** proposal for approval. No code written yet.
**Author:** Claude (Cowork session, 2026-08-20)
**Source app:** `Projects/blog-summarizer` (FastAPI + ADK + AG-UI + Claude)
**Target repo:** `Projects/LQABR`, branch currently `leadq-dev`

---

## 1. What this agent is

A **self-contained** ADK agent living at `agents/summary/` that takes *any*
input — a web URL, a raw JSON payload, or a call to another FastAPI/HTTP
endpoint — normalises it into one document shape, produces a structured
summary with Claude, and writes the result to HubSpot **through the HubSpot
MCP container it connects to at runtime**.

Two hard constraints from you, and how they are honoured:

| Constraint | How the plan honours it |
|---|---|
| Same logic/functionality as the blog-summarizer app | Every module is ported, not rewritten — crawler, ADK runner, FastAPI surface, AG-UI endpoint, React UI. See the migration map (§7). |
| Zero dependency on the rest of the repo | Nothing under `agents/summary/` imports `lqabr_core` or `mcp.*`. It ships its own `packages/summary_core`, its own tests, its own `pytest.ini`, its own Dockerfile and its own deploy scripts. A unit test enforces this (§6). |

The only runtime coupling is the one you specified: an **MCP JSON-RPC
connection** to the HubSpot MCP container (local or Cloud Run), addressed by
env var. That is a URL, not an import — it can be patched, redeployed or
swapped without touching this agent.

### Where it fits the existing flow

`agents/gateway/config/agents_registry.yaml` **already** carries the route:

```yaml
- id: R-blog-summary
  subscription_type: ticket.propertyChange
  property: blog_summary
  match: non_empty
  agent: research
```

So a summary landing on a Ticket's `blog_summary` is an existing campaign
trigger: gateway → audience expansion by `blog_industry` → research agent →
`lead_context` → email agent. This agent becomes the front of that chain.
(One blocking gap on that write — see **D-1** in §9.)

---

## 2. Folder layout — everything under one folder

```
agents/summary/
├── README.md                     what it is, how to run it, 60 seconds
├── CLAUDE.md                     agent-local rules (the standalone contract)
├── VERSION
├── pyproject.toml                packages = ["packages/summary_core", "src"]
├── requirements.txt              runtime pins (google-adk, litellm, fastapi, …)
├── requirements-dev.txt          pytest, httpx, respx
├── .env.example                  every LQABR_SUMMARY_* var, documented
├── Dockerfile                    its OWN image — not infra/gcp/cloud-run/Dockerfile
├── .dockerignore
│
├── packages/
│   └── summary_core/             the agent's private shared library
│       ├── types.py              SourceSpec, NormalizedDocument, SummaryResult
│       ├── settings.py           one typed read of the environment
│       ├── secrets.py            Secret Manager + env fallback (own copy)
│       ├── obs.py                run / process / audit streams (slim)
│       ├── sources/              THE INPUT ABSTRACTION
│       │   ├── base.py           SourceAdapter protocol + registry
│       │   ├── web.py            ported crawl_blog + SSRF guard
│       │   ├── json_source.py    raw JSON / inline payload
│       │   ├── api.py            call any FastAPI/HTTP endpoint, select a field
│       │   └── text.py           plain-text passthrough
│       └── mcp/
│           ├── client.py         MCP JSON-RPC client (handshake, tools/call, retries)
│           └── hubspot.py        thin, named wrappers over the server's tools
│
├── src/
│   ├── agent.py                  root_agent  → adk web | run | api_server
│   ├── summary_agent.py          the deterministic pipeline + ADK Runner path
│   ├── tools.py                  ADK FunctionTools (fetch_source, summarize, write_to_hubspot)
│   ├── schema.py                 request/response + summary output schema (pydantic)
│   ├── prompts/                  instruction text, versioned as files
│   ├── service_app.py            FastAPI: /summary/run, /health, /healthz, /
│   └── agui.py                   AG-UI /chat mount (feature-flagged)
│
├── ui/                           ported React/Vite frontend (backend URL from env)
│
├── tests/
│   ├── pytest.ini                its own config — runs standalone
│   ├── conftest.py               fake MCP server, mocked HTTP, no network
│   ├── test_sources.py
│   ├── test_mcp_client.py
│   ├── test_summary_agent.py
│   ├── test_service_app.py
│   └── test_standalone.py        import-guard: fails if lqabr_core / mcp.* appear
│
├── infra/
│   ├── config.sh                 its own vars; repo config.sh is NOT edited
│   ├── 01_secrets.sh             lqabr-summary-* secrets
│   ├── 02_build_push.sh          Cloud Build → Artifact Registry
│   ├── 03_deploy_run.sh          Cloud Run: lqabr-summary-agent
│   ├── cloudbuild.yaml
│   └── docker-compose.yml        local: summary agent + hubspot-mcp container
│
└── docs/
    ├── DESIGN.md                 this plan, as-built
    ├── API.md                    OpenAPI for /summary/run
    ├── ENV_VARS.md
    └── RUNBOOK.md                local run, cloud deploy, failure playbook
```

Deliberately **not** touched: `packages/lqabr_core`, `mcp/`, `tests/pytest.ini`,
`infra/gcp/config.sh`, `infra/gcp/05_deploy_agents.sh` (which is already broken
for `text_voice` — CLAUDE.md §10).

---

## 3. The input abstraction (URL | JSON | API | text)

One request shape covers all four:

```jsonc
{
  "source": {
    "kind": "url",                       // url | json | api | text
    "url":  "https://spring.io/blog/…",  // kind=url
    "payload": { "...": "..." },         // kind=json  — raw JSON, any shape
    "endpoint": "http://svc/api/x",      // kind=api
    "method": "GET",                     // kind=api
    "headers": { "Authorization": "…" }, // kind=api
    "select": "$.data.article.body",     // kind=api|json — pick a field, optional
    "text": "…"                          // kind=text
  },
  "hubspot": { "object_id": "3287919…", "object_type": "ticket" },
  "options": { "max_chars": 50000, "style": "technical" }
}
```

Every adapter returns the same `NormalizedDocument`:

```python
NormalizedDocument(source_kind, source_ref, title, text, metadata, fetched_at, truncated)
```

The summariser sees only that — so it behaves identically whatever the input
was, and **adding a fifth source later is one new adapter file plus one
registry line**, with no change to the agent, the tools or the HTTP surface.

Guards on the fetching adapters (`url`, `api`): http/https only, DNS-resolved
private/link-local/loopback addresses refused unless explicitly allowlisted
(`LQABR_SUMMARY_ALLOWED_HOSTS`), 15 s timeout, 50 000-char cap, 3 tries with
exponential backoff on 429/5xx — the house retry contract.

---

## 4. Summarisation

- Model **config-driven**: `LQABR_SUMMARY_MODEL`, default
  `anthropic/claude-sonnet-5` via LiteLLM (as blog-summarizer runs today);
  `LQABR_SUMMARY_TEMPERATURE` alongside it. Key resolved from Secret Manager /
  `.env` — never hard-coded, never pasted into chat.
- Output is a **validated schema**, not free prose:
  `title, topic, summary, key_points[], concepts[], technologies[], industry,
  takeaways[]`. Invalid model output is retried once, then returned as an
  explicit error — the agent never fabricates a summary, and never writes a
  half-parsed one to the CRM.
- The instruction is generalised past "blog": it summarises whatever document
  the adapter produced, and is told which `source_kind` it came from.

---

## 5. HubSpot write — runtime MCP connection

The agent speaks the **MCP protocol over JSON-RPC** to the containerised
HubSpot MCP, exactly as `agents/text_voice/src/mcp_client.py` already does:

```
initialize (protocolVersion 2025-06-18)  →  capture Mcp-Session-Id
notifications/initialized
tools/list                               →  bind + assert the tools we need exist
tools/call { name, arguments }           →  3 tries, backoff on 429/5xx,
                                            re-initialize on a dropped session
```

Addressing: `LQABR_SUMMARY_MCP_BASE_URL` (e.g. `http://hubspot-mcp:8080/mcp`
locally via compose, the Cloud Run URL in cloud), plus
`LQABR_SUMMARY_MCP_AUTH_TOKEN` if the container is authenticated.

**We follow the tools and schema that `mcp/hubspot` already defines** — the
agent invents no HubSpot property names and no new tool of its own. Property
names it does reference (`blog_summary`, `blog_industry`) come from the
gateway registry and stay env-overridable, per repo convention.

A `tools/list` assertion at startup means a mismatch between this agent and the
MCP container surfaces as a loud, named startup failure rather than a silent
no-write at runtime.

---

## 6. ADK standards conformance

Matches the other LQABR agents, minus the shared bits you excluded:

- `src/agent.py` exposes `root_agent`, so `adk web | run | api_server
  agents/summary/src` all work.
- Deterministic tool logic (`sources/`, `mcp/`, pipeline) is typed and mockable
  and lives **separately** from the ADK/model wrapper — same discipline as
  `lead_profile`.
- `service_app.py` gives it a domain surface the gateway/orchestrator can call
  directly, with `/health` **and** `/healthz` returning identical payloads.
- Bad input is flagged with a reason, never dropped: `unresolved`,
  `write_failures` lists on the response.
- One image, one service, one port; zero instances at rest.

**All three surfaces, as you asked**, chosen by `LQABR_SUMMARY_ROUTES=all`:

| Surface | Path | Purpose |
|---|---|---|
| ADK api_server | `POST /run` (`adk api_server`) | ADK-native / dev console |
| Domain API | `POST /summary/run` (+ A2A `message/send` envelope accepted) | gateway, orchestrator, other agents |
| AG-UI | `POST /chat` (`ag_ui_adk`) | the ported React UI, streaming |

`test_standalone.py` walks every file under `agents/summary/` and fails the
build on any `lqabr_core` or `mcp.` import — the guarantee is mechanical, not
a convention.

---

## 7. Migration map from `blog-summarizer`

| From | To | Change |
|---|---|---|
| `app/crawler.py` | `packages/summary_core/sources/web.py` | + SSRF guard, retries, typed return; 50 k cap kept |
| `app/agent.py` | `src/agent.py` | model from env not literal; instruction generalised; tools list widened |
| `app/agent_service.py` | `src/summary_agent.py` | Runner/session kept; a deterministic path added so API calls skip the session handshake |
| `app/fast_api_app.py` | `src/service_app.py` + `src/agui.py` | `/health` kept; CORS origins from env; AG-UI mount flagged; A2A envelope accepted |
| `app/models.py` | `src/schema.py` | `BlogRequest` → `SummaryRequest` (4 source kinds); `BlogSummaryResponse` → `SummaryResult` + HubSpot write result |
| `test_agui.py`, `test_crawler.py`, `test_anthropic.py` | `tests/` | become real pytest tests with mocks — no live network, no live API key |
| `frontend/` | `ui/` | unchanged; backend URL from env instead of hard-coded localhost |
| `pyproject.toml` | `agents/summary/pyproject.toml` | same deps, pinned; `ag-ui-adk`, `ag-ui-protocol` retained |

Nothing is dropped. The blog-specific *wording* becomes source-agnostic; the
blog-specific *behaviour* survives as the `url` adapter.

---

## 8. Build phases

| Phase | Work | Output |
|---|---|---|
| **P0** | Scaffold, `pyproject`, requirements, `.env.example`, import-guard test | folder exists, `pytest` green on 1 test |
| **P1** | `summary_core`: types, settings, secrets, obs, 4 source adapters | sources tested offline |
| **P2** | MCP client + HubSpot wrappers + fake-MCP fixture | write path tested without a container |
| **P3** | `tools.py`, `agent.py`, `summary_agent.py`, `schema.py` | `adk run agents/summary/src` works locally |
| **P4** | `service_app.py`, A2A envelope, AG-UI mount, `ui/` port | all three surfaces answer |
| **P5** | Test suite to green (~30–35 tests) + `docs/` | `pytest -c agents/summary/tests/pytest.ini -q` green |
| **P6** | Dockerfile, cloudbuild, deploy script, docker-compose with the MCP container | image builds, runs locally against the MCP |
| **P7** | Verification: local run → MCP container → HubSpot (dry-run first), then the gateway `R-blog-summary` hop | evidence captured in `docs/RUNBOOK.md` |

Per CLAUDE.md §7/§8: work starts only after the Jira ticket exists and is moved
to In Progress, on a branch `LQABR-<ticket>-summary-agent` off the Epic branch.
Nothing is pushed and no PR is opened without your explicit go-ahead.

---

## 9. Open decisions — I need these before P2/P3

**D-1 · The Ticket write is blocked in the central MCP today (important).**
`mcp/hubspot/schema.py` allowlists **contact** properties only
(`WRITABLE_CONTACT_PROPERTIES`), and `crm.patch_object` patches contacts. There
is no ticket write path and `blog_summary` / `blog_industry` are not in the
allowlist — so `post_patch_crm` would reject the write the gateway route is
waiting for. Options:
  a. Extend `mcp/hubspot` with `WRITABLE_TICKET_PROPERTIES` + a ticket patch
     (a small, central change — the right place, but it edits shared code);
  b. the summary agent writes to a **contact** property instead (no central
     change, but it does not fire `R-blog-summary`);
  c. the containerised MCP you run already exposes a ticket write the repo copy
     doesn't — in which case I bind to it and change nothing.
  → I'd need `tools/list` from your running container to tell (c) apart.

**D-2 · Exact wire tool names.** In-process names are
`get_lead_profile_details` / `list_trigger_leads` / `post_patch_crm`; `text_voice`
calls the wire names `get_lead_profile` / `upsert_lead_profile`. I'll bind to
whatever `tools/list` returns from your container — one paste of that output
removes the guesswork.

**D-3 · Who owns the object id?** Does `/summary/run` receive an existing
Ticket id, or should the agent create the Ticket first? Nothing in the repo
creates it today.

**D-4 · Jira + branch.** Ticket key and parent Epic for this work.

**D-5 · Root test suite.** Keep `agents/summary/tests` out of
`tests/pytest.ini` (true standalone), or add one line so CI runs it too? My
recommendation: keep it out, and have the agent's own CI step run it.

---

## 10. Two things I noticed in passing

- `CLAUDE.md` §2 describes six core agents (`ingestion`, `lead_profile`,
  `email`, `text_voice`, `scheduling`, `orchestrator`); the working tree has
  four — `email`, `gateway`, `lead_profile`, `text_voice`. Worth reconciling
  before the doc is used as onboarding truth.
- The tree is on `leadq-dev` with `CLAUDE.md` modified and uncommitted.
