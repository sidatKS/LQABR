# Summary Agent — design, as built

## What it does

One job, four ways in:

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

The adapters are the whole trick: by the time anything is summarised it is a
`NormalizedDocument`, so the model behaves identically whatever the input
was, and a fifth input kind is a new module plus one registry line — not a
change to the agent, the tools or the API.

## Where it sits in the LQABR flow

A summary written to a Ticket's `blog_summary` is an existing campaign
trigger. The gateway's `R-blog-summary` route expands it by `blog_industry`
into one research hand-off per lead; research writes `lead_context`; the
email agent takes it from there. This agent is the front of that chain and
knows nothing about the rest of it — it writes a property and stops.

Writing without the gateway is equally valid: the write is the agent's job,
the fan-out is the gateway's.

## Standalone, and what that costs

Nothing under `agents/summary/` imports `lqabr_core` or the repo-root `mcp`
package. It has its own library, tests, image and deploy scripts, so it can
be patched and shipped without a repo-wide change — and a repo-wide change
cannot break it. `tests/test_standalone.py` enforces this by walking every
Python file, and the Dockerfile enforces it physically: the build context is
`agents/summary`, so the shared folders are not even reachable.

The cost is duplication — settings, secrets and observability exist here in
small, purpose-built forms. That is the deliberate trade: a copy that drifts
is a smaller problem than a shared package that cannot be upgraded alone.

## Modules

| Module | Responsibility |
|---|---|
| `summary_core/types.py` | `SourceSpec`, `NormalizedDocument`, `SummaryResult`, `WriteResult` |
| `summary_core/settings.py` | the only place that reads `os.environ` |
| `summary_core/secrets.py` | env / Secret Manager / auto; logs name + source, never a value |
| `summary_core/obs.py` | process, audit and system streams; name-based redaction |
| `summary_core/sources/` | the four adapters, the SSRF guard, the retry policy, the selector |
| `summary_core/mcp/client.py` | MCP JSON-RPC: handshake, discovery, `tools/call`, session recovery |
| `summary_core/mcp/hubspot.py` | the only HubSpot-shaped code in the agent |
| `src/schema.py` | the wire contracts and the model-output validator |
| `src/summarizer.py` | the model call; injectable, so the suite needs no key |
| `src/tools.py` | the ADK tools — plain functions, no ADK types |
| `src/pipeline.py` | the deterministic run: fetch → summarise → write |
| `src/agent.py` | `root_agent` for `adk web/run/api_server`, plus the CLI |
| `src/service_app.py` | the HTTP surface |

## Two paths, one behaviour

`pipeline.run_summary` calls the steps in order; `root_agent` lets the model
choose the tools. They share the prompt (`src/prompts/summarize.md`), the
validator (`schema.parse_summary`) and the write (`summary_core.mcp`), so
they cannot drift into producing different summaries for the same document.
The API uses the pipeline so a caller does not pay for a session handshake
and a tool-choice loop to get a summary.

## Decisions worth remembering

**Nothing external is hard-coded.** Tool names, tool ARGUMENT names, HubSpot
property names, the object type, the model and the MCP URL are all
environment variables with defaults. A rename anywhere out there is a config
change. On top of that, the tool names are *discovered* (`tools/list`) at
startup and checked — a mismatch is a named error at boot, not a write that
quietly did nothing.

**A failed write is never a success.** `WriteResult.status` distinguishes
`written` / `dry_run` / `skipped` / `error`, the HTTP response's `status`
follows it, and the pipeline returns the summary alongside the failure so
the work is not lost.

**The model never invents.** An answer that fails validation is retried once
with the validator's own complaint; a second failure fails the run. An empty
field is correct output; a guessed one is not.

**Fetching is guarded.** http/https only, DNS resolved, private / loopback /
link-local refused unless allowlisted. `169.254.169.254` is the one that
matters: it hands out service-account tokens to anything that can make the
agent issue a GET.

**The MCP being asleep at boot is normal.** It is a separate container that
scales to zero, so the startup check records its outcome on `/health` rather
than killing the service. `LQABR_SUMMARY_MCP_STARTUP_CHECK=strict` is the
production setting once the MCP is always-on.

## Open item

The central `mcp/hubspot` in this repo allowlists CONTACT properties
(`WRITABLE_CONTACT_PROPERTIES`) and patches contacts, so a Ticket write of
`blog_summary` would be refused by `post_patch_crm` as that code stands. This
agent is built to write through whatever the CONTAINER exposes; `GET
/mcp/tools` on a running instance is the one-call answer to whether a ticket
write is there. If it is not, the fix belongs in the central MCP (a
`WRITABLE_TICKET_PROPERTIES` set plus a ticket patch), not here.
