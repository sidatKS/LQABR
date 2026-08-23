# Summary Agent — build report (P0–P7)

**Built:** 2026-08-20 · **Location:** `agents/summary/` on branch `leadq-dev`, untracked
**Tests:** 189 passing, fully offline (no API key, no network, no MCP container)
**Source app:** `blog-summarizer` (FastAPI + ADK + AG-UI + Claude), ported whole

---

## What was built

A standalone ADK agent that summarises **a web URL, a raw JSON payload, another
FastAPI/HTTP endpoint, or plain text**, and writes the result to HubSpot by
dialling the HubSpot MCP container at runtime over MCP JSON-RPC.

```
  kind=url    a web page              ┐
  kind=api    another HTTP/FastAPI    ├─► NormalizedDocument ─► Claude ─► SummaryResult
  kind=json   a raw JSON payload      │        (one shape)                    │
  kind=text   plain text              ┘                                       │
                                                                              ▼
                                        HubSpot MCP container ◄── MCP JSON-RPC (runtime)
                                                    │
                                                    ▼
                                              HubSpot CRM
```

A summary landing on a Ticket's `blog_summary` is the existing `R-blog-summary`
campaign trigger: gateway → `blog_industry` fan-out → research → `lead_context`
→ email. This agent is the front of that chain and knows nothing about the rest.

---

## The standalone contract, enforced not promised

Nothing under `agents/summary/` imports `lqabr_core` or the repo-root `mcp`
package. Its own library, tests, `pytest.ini`, Dockerfile and deploy scripts.

Three independent enforcements:

1. `tests/test_standalone.py` walks every `.py` file and fails on a forbidden
   import; also checks both requirements files for repo paths and editable
   installs, and the Dockerfile for `COPY mcp` / `COPY packages/lqabr_core`.
2. The Docker **build context is `agents/summary`**, so the shared folders are
   not even reachable at build time.
3. `infra/` is the agent's own — `infra/gcp/config.sh` and `05_deploy_agents.sh`
   are untouched, so deploying this agent changes nothing for any other.

The cost is duplication: settings, secrets and observability exist here in
small purpose-built forms. Deliberate — a copy that drifts is a smaller
problem than a shared package that cannot be upgraded alone.

---

## Flexibility, as requested

Nothing external is hard-coded. Every one of these is an environment variable
with the current value as its default:

| What | Variable |
|---|---|
| MCP tool names | `LQABR_SUMMARY_MCP_TOOL_READ` / `_WRITE` / `_LIST_LEADS` |
| MCP tool **argument** names | `LQABR_SUMMARY_MCP_ARG_OBJECT_ID` / `_ARG_PROPERTIES` |
| HubSpot properties | `LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY` / `_INDUSTRY_PROPERTY` |
| Object type | `LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE` |
| MCP endpoint, model, routes, limits | see `docs/ENV_VARS.md` |

On top of that the tool names are **discovered** — `tools/list` at startup,
bound, and asserted. A config/server mismatch is a named error at boot naming
both what was configured and what the server actually has, plus which variable
to change. `GET /mcp/tools` answers the same question live on a running
instance. A rename is a config change; there is no code path to edit.

---

## Layout

```
agents/summary/            81 files, 35 Python
├── packages/summary_core/ types, settings, secrets, obs
│   ├── sources/           url | api | json | text adapters, SSRF guard, retries, selector
│   └── mcp/               the JSON-RPC client + the only HubSpot-shaped code
├── src/                   schema, summarizer, tools, pipeline, agent, service_app, prompts
├── tests/                 189 tests, own pytest.ini, fully offline
├── infra/                 Dockerfile deps, config.sh, secrets, build, deploy, compose
├── docs/                  DESIGN · API · ENV_VARS · RUNBOOK
└── ui/                    the ported React/AG-UI front end
```

**All three surfaces ship:** `adk web|run|api_server agents/summary/src`;
`POST /summary/run` plus the gateway's A2A `message/send` envelope on
`/summary/a2a`; and AG-UI `/chat` for the ported UI. One image, one service,
one port — `LQABR_SUMMARY_ROUTES` selects.

---

## Behaviour worth knowing

* **A failed write is never a success.** `written` / `dry_run` / `skipped` /
  `error` are distinct; the HTTP `status` follows the write, and the summary
  is still returned so the work is not lost.
* **The model never invents.** Invalid output is retried once with the
  validator's own complaint; a second failure fails the run. Empty fields are
  correct output, guessed ones are not.
* **Fetching is guarded.** http/https only, DNS resolved, private/loopback/
  link-local refused unless allowlisted — `169.254.169.254` hands out
  service-account tokens to anything that can make the agent issue a GET.
  A bare string carrying any URI scheme (`file://`, `ftp://`) is treated as a
  URL so the guard refuses it by name, rather than being summarised as prose.
* **A provider outage is a reported failure**, not a 500 with a provider stack
  trace: `"the model call failed (AuthenticationError): …"` with the step named.
* **The MCP asleep at boot is normal** — it scales to zero. The startup check
  records its outcome on `/health` rather than killing the service;
  `LQABR_SUMMARY_MCP_STARTUP_CHECK=strict` is the production setting once the
  MCP is always-on.
* **Dry run first.** `infra/config.sh` and `docker-compose.yml` both default to
  `LQABR_SUMMARY_DRY_RUN=1`: the write is computed and logged, nothing reaches
  the CRM until you flip it.

---

## Verification done

| Check | Result |
|---|---|
| Full suite, offline | **189 passed** |
| Real-socket end-to-end (`test_e2e_local.py`) | a real HTTP server speaking MCP JSON-RPC + the real FastAPI app: handshake, `Mcp-Session-Id` round trip, content-block unwrapping, write payload asserted |
| Standalone import guard | 35 files scanned, zero forbidden imports |
| Boot from the image file set | `/health` and the route index answer with only what the Dockerfile copies, on the image's `PYTHONPATH` |
| Live uvicorn over a socket | refused source → `status: failed` with the reason; `/mcp/tools` with no MCP → `502` |
| ADK wiring | `root_agent` builds; tools `fetch_document`, `get_lead_profile`, `write_summary_to_hubspot`; model `anthropic/claude-sonnet-5` |
| Shell + YAML | all `infra/*.sh` parse; compose and cloudbuild valid |

Not done, because it needs your environment: `docker build` (no daemon in this
sandbox — the file set was verified instead) and any run against the real MCP
container or portal.

---

## Open items for you

1. **The Ticket write is still blocked in the central MCP.** `mcp/hubspot/schema.py`
   allowlists CONTACT properties (`WRITABLE_CONTACT_PROPERTIES`) and
   `crm.patch_object` patches contacts, so `blog_summary` on a Ticket would be
   refused by `post_patch_crm` as that code stands. This agent writes through
   whatever the container exposes — run `GET /mcp/tools` against your live MCP
   to see whether a ticket write is there. If not, the fix belongs in the
   central MCP (`WRITABLE_TICKET_PROPERTIES` + a ticket patch), not here.
2. **Confirm the wire tool names** from the container, then set the three
   `LQABR_SUMMARY_MCP_TOOL_*` variables if they differ. No rebuild needed.
3. **Who creates the Ticket?** Nothing in the repo creates it today; the agent
   currently expects an existing `object_id`.
4. **Jira ticket + branch** — per CLAUDE.md §7/§8 this work should sit on
   `LQABR-<ticket>-summary-agent` off the Epic branch before any push.
5. **Root test suite:** `agents/summary/tests` is deliberately not in
   `tests/pytest.ini`. Add one line if CI should run it centrally.

Also noticed: `CLAUDE.md` §2 describes six core agents; the tree has four
(`email`, `gateway`, `lead_profile`, `text_voice`) plus this new one.

---

## First run, when you are ready

```bash
cd agents/summary
HUBSPOT_MCP_IMAGE=<your-image> ANTHROPIC_API_KEY=… \
  docker compose -f infra/docker-compose.yml up --build

curl localhost:8080/mcp/tools | jq .      # ← settles items 1 and 2 above
curl -sX POST localhost:8080/summary/run -H 'Content-Type: application/json' \
     -d '{"source":"https://spring.io/blog","hubspot":{"object_id":"<ticket-id>"}}' | jq .
```

Compose defaults to dry run. `docs/RUNBOOK.md` has the deploy sequence and a
failure-reading table.
