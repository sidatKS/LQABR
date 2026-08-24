# agents/summary — Summary Agent

Turns **any** input into a structured summary and lands it on HubSpot.

    a web URL          ┐
    a raw JSON payload ├─► NormalizedDocument ─► Claude ─► SummaryResult ─┐
    an HTTP/FastAPI    │                                                  │
    plain text         ┘                                                  │
                                                                          ▼
                                         HubSpot MCP container  ◄── MCP JSON-RPC
                                                    │
                                                    ▼
                                              HubSpot CRM

Written on a Ticket's `blog_summary`, the result is an existing campaign
trigger: the gateway route `R-blog-summary` expands it by `blog_industry`
into one research hand-off per lead, research writes `lead_context`, and the
email agent takes it from there. This agent is the front of that chain and
knows nothing about the rest of it.

## Standalone — on purpose

This agent imports **nothing** from the rest of the repo. No `lqabr_core`,
no `mcp/hubspot`, no shared `tests/`, no shared `infra/`. It carries its own
library (`packages/summary_core`), its own tests, its own Dockerfile and its
own deploy scripts, so it can be patched or upgraded without a repo-wide
change — and a repo-wide change cannot break it.

The single coupling is a **runtime** one: it dials the HubSpot MCP container
at `LQABR_SUMMARY_MCP_BASE_URL`. That is a URL, not an import.
`tests/test_standalone.py` fails the build if that ever stops being true.

## Layout

    packages/summary_core/   this agent's private library
      sources/               url | json | api | text adapters
      mcp/                   the MCP client that dials the HubSpot container
    src/                     the ADK agent + its HTTP surfaces
    tests/                   its own suite, its own pytest.ini, fully offline
    infra/                   its own image + deploy scripts
    docs/                    design, API, env vars, runbook
    ui/                      the React front end (AG-UI /chat)

## Run it

    cp .env.example .env                 # then fill in the model key
    pip install -r requirements-dev.txt

    adk web agents/summary/src           # the agent in the ADK UI
    adk run agents/summary/src           # one run in the terminal
    uvicorn service_app:app --port 8080  # from src/ — the domain API

    python3 -m pytest -c agents/summary/tests/pytest.ini -q

## Status

**P0-P7 complete.** 189 tests green, all offline:

    python3 -m pytest -c agents/summary/tests/pytest.ini -q

`tests/test_e2e_local.py` is the one that matters most — it stands up a real
HTTP server speaking MCP JSON-RPC and drives the real FastAPI app against it,
so the wire format is proven, not assumed. Only the model is faked.

Not yet done, because it needs your running container and portal:

* one run against the real HubSpot MCP (`GET /mcp/tools` first — see
  `docs/RUNBOOK.md`),
* the Ticket write itself: the repo's central `mcp/hubspot` allowlists
  CONTACT properties only, so `blog_summary` on a Ticket would be refused by
  `post_patch_crm` as that code stands. This agent writes through whatever
  the container exposes; `/mcp/tools` on a live instance is the one-call
  answer.

See `docs/DESIGN.md` for how it works and `docs/RUNBOOK.md` for how to run it.
