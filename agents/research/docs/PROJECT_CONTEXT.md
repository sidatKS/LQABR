# Research Agent — project context

Everything a new session needs to work on **this agent only**. The other LQABR
agents (email, text_voice, summary, scheduling, gateway) are out of scope here;
they are referenced only where this agent touches them.

Repo: `LQABR`, branch `leadq-dev-rao`. Agent root: `agents/research/`.

---

## 1. What it does

One published blog post becomes personalised `lead_context` on **every HubSpot
contact in that post's industry**.

```
Marketing publishes a post   (HubSpot Ticket: blog_summary + blog_industry)
   -> Agent Gateway routes it (hands over ONLY the post's objectId)
   -> read the post           (MCP: get_blog_summary)
   -> take its industry       (blog_industry)
   -> list the leads          (direct HubSpot read — the one exception)
   -> per lead: research + write lead_context   (web search + model, then MCP)
```

That final write raises HubSpot's `contact.propertyChange` on `lead_context`,
which the gateway routes to the **Email agent**. There is no queue and no
scheduler between the two agents: the CRM write *is* the trigger.

## 2. Where things are

```
agents/research/
  src/
    service_app.py     FastAPI surface — 4 POST routes + health
    pipeline.py        run_research() one lead · run_campaign() one post
    composer.py        prompt assembly + strip_preamble()
    schema.py          pydantic edge types, A2A envelope parsing
    agent.py           one-lead CLI
    prompts/research.md   the model contract (a file, not a string literal)
  packages/research_core/     the agent's own library — deliberately NOT shared
    settings.py        every knob, resolved once from env > config.yaml > default
    obs.py             three log streams + the console formatter
    secrets.py         env | secret_manager | auto resolution
    hubspot_direct.py  THE one direct HubSpot call (read-only, test-enforced)
    mcp/client.py      JSON-RPC over streamable HTTP
    mcp/hubspot.py     the four tool wrappers
    search/anthropic_search.py   model call + server-side web_search
  tests/               87 tests, fully offline
  docs/                SETUP.md · RUNBOOK.md · API.md · ENV_VARS.md · DESIGN.md
  setup_env.sh         one-command local setup from Secret Manager
```

**Standalone on purpose.** It imports nothing from `packages/lqabr_core` and
nothing from another agent. `pip install -r requirements.txt` is the whole
install. A test (`test_standalone.py`) enforces it.

## 3. The four routes, and why there are four

| Route | Scope | Delivery | Caller |
|---|---|---|---|
| `POST /research/run` | one contact | synchronous | hand-driven |
| `POST /research/campaign` | one post → its industry | synchronous | a job/script |
| `POST /research/a2a` | one contact | ack, then background | gateway |
| `POST /research/campaign/a2a` | one post → its industry | ack, then background | gateway |

Two axes, not one: **scope** (one lead vs a whole industry) and **delivery**
(wait for the answer vs acknowledge inside the gateway's ~5s budget).

**What the gateway actually sends** — JSON-RPC on the outside, HubSpot's own
event verbatim in `params.metadata`, so the metadata is **camelCase**
(`objectId`, `subscriptionType`, `attemptNumber`). Both spellings are read
everywhere. The exact payloads are in `docs/GATEWAY_PAYLOADS.md`; keep that
file current when the shape changes.

**The two a2a routes differ by what the id IS.** On `/research/a2a` the
`objectId` is a CONTACT; on `/research/campaign/a2a` it is a blog POST. Sending
a post to the contact route fails at `read_lead` every time, because a Ticket is
not a lead. They are separate routes rather than one handler guessing.

## 4. Decisions worth not re-litigating

**The MCP is the only door to HubSpot — with exactly one exception.**
Every read and write goes through the central MCP container
(`tne736/lqabr-mcp-server:latest`, port 8080, 4 tools). The exception is
"which leads are in this industry": the MCP has no listing tool, so
`hubspot_direct.py` calls HubSpot directly. It is read-only and a test asserts
the file contains no `PATCH`/`PUT`/write verb. `test_standalone.py` also asserts
no *other* file talks to `api.hubapi.com`.

**Industry lives on the Company, not the Contact.** So the lookup is two steps:
search companies where `industry == X`, then each company's associated contacts,
then dedup.

**"Could not ask" is not "nobody matched".** If the industry lookup fails, the
campaign refuses to run. Reporting a clean campaign that silently skipped every
lead would be worse than failing.

**Status follows the WRITE, not the research.** A note that was composed but
could not be landed is `failed` — and the note still comes back in the response
so the work is not lost.

**One lead's failure never stops the campaign.** Each lead is worked
independently; a failure is recorded against that lead with a named reason
(`bad-data:` / `crm-error:`) and the loop continues. The response carries
`leads_found` / `written` / `failed` / `skipped` plus a per-lead breakdown, and
`status` can be `partial`.

**The blog is read once per campaign**, then passed down to every lead —
otherwise N leads would fetch the same post N times.

**Both credentials come from Secret Manager at run time, by NAME.** Neither the
model key nor the HubSpot token is written to `.env`. `ANTHROPIC_API_KEY` still
works as a local override, but its use is logged, so a run on a stale key is
never silent. Consequence: valid ADC is required to *run*, not just to set up.

**`skip_if_context_present` defaults to false** — a re-run overwrites. Set it
true to make a campaign resumable.

## 5. Things that bit us, and the fix

| Symptom | Cause | Fix |
|---|---|---|
| Every lead: `ANTHROPIC_API_KEY is not set` | `.env` sourced *after* the key was exported, blanking it | source `.env` FIRST; key now from Secret Manager |
| Everything "succeeds", HubSpot unchanged | `LQABR_RESEARCH_DRY_RUN=1` | check `dry_run` in `/health` |
| Note contained `"I'll search for current information about…"` | `_collect()` concatenated **every** text block; with server-side search the model narrates between calls | keep only text after the LAST search block |
| Note began `"Here is the lead_context note:"` after a recap paragraph | model writes a recap, *then* announces the note | `strip_preamble()` takes what follows the LAST announcement line |
| `POST /research/run` silently ignored `summary_objectId` | `resolved()` read it only from the nested target | mirrored like `objectId` |
| The CLI could never succeed | required `--blog-published-at`, which stopped being the blog key | `--summary-object-id` |
| CLI output unparseable | logs and the result JSON on the same stream | logs to stderr; stdout is the result document |
| `2 validation errors for call[get_blog_summary]` | the MCP container is an **older image** keyed on `blog_published_at` | `docker pull` and recreate the container |
| Log lines corrupting mid-run | a line longer than the terminal wrapped and redrew over its neighbour | measure against real terminal width; diagnostics continue on indented lines |
| Campaign reported `no blog summary found` for a post that exists | the MCP answered `found:false` **with** `status:halted` / `failure_kind:systemic` — it could not read its own HubSpot token — and the read treated that as a plain not-found | `refusal()` tells a refusal from an absent record; the reason rides `HubSpotMCP.last_error` into the reported error. "Could not ask" is not "nobody matched" — now on the reads too |
| Gateway hand-off `rejected: payload carries no objectId` — on a payload that has one | `params.metadata` is HubSpot's own event, spelled `objectId`; only `objectId` was read | `A2AEnvelope._from_meta` reads either spelling, metadata first, then the top level |
| A post sent to the contact route died at `read_lead` as `crm-error: no lead` | the id resolves on both routes, so nothing noticed the record kind | `subscriptionType` -> `record_kind()`; the wrong route now refuses at the door and names the right one |
| One campaign froze the whole process, `/health` included | `research_run` / `research_campaign` / `mcp_tools` were `async def` doing blocking I/O, so they ran ON the event loop | plain `def` — Starlette offloads those to a threadpool (`/health` 2.05s → 0.06s under load) |
| `secret_resolved` printed `secret=<redacted>` — the one field it exists for | the redactor matched substrings, so it blanked the credential's NAME as well as its value | redact value-holders only; `_secret` / `_source` / `secret` / `credential` are identifiers and print |
| A lead the write tool could not accept reported `status: completed, error: ""` | `WriteResult.ok` treated a bad-data skip the same as "nothing needed doing" | new `not_writable` status; `skipped` now means only `skip_if_context_present` |
| `limit: "all"` from the gateway was a 500 | `int()` on webhook metadata inside the route handler | `A2AEnvelope._int`, bounded 1..1000 |
| A background run that raised left no record at all | `background.add_task(runner, ...)` — the gateway already had `accepted` | `_guarded()` emits `run_crashed` with the reason, then re-raises |
| `LQABR_RESEARCH_SEARCH_ENABLED=0` still ran (and billed) 5 searches per lead | the web-search tool was attached unconditionally | the tool is attached only when the flag is on |
| A deploy without `prompts/research.md` wrote notes from two sentences and reported `completed` | `load_system_prompt` caught `OSError` and substituted a 141-char fallback for the 1,496-char contract | it raises now — the prompt file IS the contract, and a broken deployment fails on the first lead |
| Every response reported `searches: 0` | the count was computed, carried and logged, then dropped at the Composer boundary — `ResearchNote` had no field for it | `ResearchNote.searches`, threaded to the response |
| The MCP handshake announced version `0.1` while six literals said `0.1.0` | `VERSION` existed, was empty of readers, and every place spelled its own | `research_core.__version__` / `SERVICE_NAME`, read from `VERSION`; a test fails the build on a new literal |
| `2 validation errors for call[get_blog_summary]: objectId Missing required argument / object_id Unexpected keyword argument` | the MCP container renamed its own argument mid-day — `object_id` worked at 05:42, was rejected by 08:42 | the name is `settings.mcp_object_id_arg` (`LQABR_RESEARCH_MCP_OBJECT_ID_ARG`, default `objectId`), never a literal. The next rename is a config flip |
| `bash\r: No such file` | CRLF line endings on a `.sh` | `.gitattributes` pins `*.sh` to LF; never write these files from Windows text mode |

## 6. Observability

Three JSON streams to stdout, one `run_id` per campaign:

- **process** — the steps: `step_in` / `step_out` around every stage, plus the
  decisions between them (`ok` / `failed` / `skipped` / `degraded`)
- **audit** — every outbound call: service, endpoint, status, attempt, ms, the
  **parameters it was made with**, and the credential's name — never its value
- **system** — startup and config

**Every step is framed** by `with obs.step(...)`: `step_in` names what it was
handed, `step_out` names what it produced and how long it took. The frame closes
itself — on an early return, and on an exception, which is recorded as the
failure it is before it propagates — so a step that opens can never be left
open. Between them sit the details:
`model_request` (model, `max_tokens`, the web-search tool and its `max_uses`,
prompt/system sizes and previews), `model_response` (stop reason, token counts,
searches, sources, a preview of the text) and `context_write_start` (the tool,
the property, and every argument, with the note as `[N chars] head…`).

Text payloads appear as length-marked `*_preview` fields — enough to see what
was asked and what came back, never a 4,000-character block in the middle of a
run. `LQABR_RESEARCH_LOG_DETAIL=0` drops previews and parameter bags.

`LQABR_RESEARCH_LOG_FORMAT` = `auto` (default) / `text` / `json`. **auto** means
readable text when stdout is a terminal, JSON when it is not — so Cloud Run
keeps its structured fields and a human gets something legible. **The log file
is always JSON, with every field, previews included.**

```
04:34:03 * campaign_start           objectId=329473274558 model=claude-sonnet-4-6 lead_lookup=hubspot_direct dry_run=False
04:34:03 > IN  read_blog            objectId=329473274558 via=mcp tool=get_blog_summary url=http://localhost:8080/mcp
04:34:03 * mcp_tool_call            tool=get_blog_summary arguments={objectId=329473274558} timeout_s=60
04:34:04 -> mcp       POST http://localhost:8080/mcp 200 1488ms tool=get_blog_summary objectId=329473274558
04:34:05 * mcp_tool_result          tool=get_blog_summary attempt=1 kind=object keys=[found, summary, ticket_hs_id +1]
           result_preview: {'found': True, 'summary': {'blog_summary': 'Grounding an AI system in clinical documents...
04:34:05 < OUT read_blog            ok 1501ms blog_industry=HEALTHCARE summary_chars=278 ticket_id=329473274558
           summary_preview: Grounding an AI system in clinical documents needs more than good retrieval: permission-aware...
04:34:05 > IN  list_leads           industry=HEALTHCARE limit=100 via=hubspot_direct
04:34:07 -> hubspot   POST https://api.hubapi.com/crm/v3/objects/companies/search 200 671ms filter=industry EQ HEALTHCARE limit=100
04:34:07 + industry_companies_found industry=HEALTHCARE count=5 companies=[339297322741, 339297323706 +3]
04:34:11 < OUT list_leads           ok 6144ms leads_found=5 leads=[533990588137, 533994194677 +3]

04:34:11 * lead 1/5    533990588137  working...
04:34:11 > IN  read_lead            objectId=533990588137 via=mcp tool=get_lead_profile
04:34:12 -> mcp       POST http://localhost:8080/mcp 200 1682ms tool=get_lead_profile objectId=533990588137
04:34:12 < OUT read_lead            ok 1688ms name=Govardhan Terli company=Health Catalyst writable_missing=none
04:34:12 > IN  research             objectId=533990588137 company=Health Catalyst model=claude-sonnet-4-6 target_words=160
04:34:14 * model_request            model=claude-sonnet-4-6 max_tokens=2000 search_tool=web_search_20250305 search_max_uses=5
           system_preview: # Research prompt - lead_context You are the Research Agent for a B2B lead-qualification...
           prompt_preview: Research this lead and write the lead_context note. ## The lead - Name: Govardhan Terli...
04:34:34 -> anthropic POST messages.create 200 20840ms model=claude-sonnet-4-6 max_tokens=2000 prompt_chars=699
04:34:34 * model_response           stop_reason=end_turn input_tokens=32807 output_tokens=657 searches=3 sources=25 chars=1899
           text_preview: Here is the `lead_context` note for Govardhan Terli at Health Catalyst: --- Health Catalyst is...
04:34:34 * compose_preamble_stripped raw_chars=1899 kept_chars=1821 dropped_chars=78
04:34:34 < OUT research             ok 22258ms chars=1821 words=262 sources=25
04:34:34 > IN  write_context        objectId=533990588137 tool=upsert_lead_profile property_name=lead_context chars=4035
04:34:34 * mcp_tool_call            tool=upsert_lead_profile arguments={employee_id=E00010, company_id=C0010,
                                    decision_maker_flag=Yes, lead_context=[4035 chars] Health Catalyst is a healthcare da...}
04:34:38 -> mcp       POST http://localhost:8080/mcp 200 3116ms tool=upsert_lead_profile employee_id=E00010 ...
04:34:38 < OUT write_context        ok 3118ms write_status=written chars=4035
04:34:38 + run_complete             status=completed write_status=written chars=4035 sources=25
04:34:38 + lead 1/5    533990588137 completed chars=4035  (4 left)


04:34:38 * lead 2/5    533994194677  working...
```

`>` IN / `<` OUT frame a step - its inputs, then its outputs and duration.
`*` a step detail - `+` succeeded - `!` degraded but continuing - `x` failed -
`->` an outbound call, with the parameters it was made with. Two blank lines
close each lead. (A UTF-8 console shows arrows and check marks; a cp1252 one
gets the ASCII above.)

**One line per fact, and only one.** A step's IN/OUT pair is the narrative;
`mcp_tool_call` / `mcp_tool_result` and the audit hop are the mechanics beneath
it. Nothing else re-states them - the layer-level `*_start` / `*_ok` echoes were
removed once the frame carried the same fields, taking ~9 lines per lead with
them.

Rules the tests enforce: secrets stay redacted in the readable form too (a token
*count* is not a token, so `max_tokens` survives); no line exceeds the terminal
width; a failure `reason` and a payload preview are never truncated away — they
continue on indented lines.

## 7. Running it

```bash
cd agents/research
./setup_env.sh                 # writes .env with NO credential in it
source ~/lqabr-venv/bin/activate
set -a && source .env && set +a
python3 -m uvicorn service_app:app --port 8086 --app-dir src
```

`./setup_env.sh --live` when you actually want writes. The MCP container must be
up on `:8080`. Full walkthrough in `docs/SETUP.md` (with a WSL appendix).

Tests — offline, no credentials, no network:

```bash
cd agents/research && PYTHONPATH=src:packages python3 -m pytest -q
```

## 8. Verified live (2026-08-24)

Blog post `330008697562` → FINANCIAL_SERVICES → 5 companies → 5 contacts → **5
written, 0 failed**, driven through `/research/campaign/a2a`. All five notes
scanned for narration markers: clean. Earlier: LEGAL_SERVICES → 2 written
(Everlaw, Litera).

## 9. Open, flagged, not done

- **No authentication on any route.** Anyone with the URL triggers live CRM
  writes across a whole industry. It is currently reachable over an ngrok
  tunnel.
- Notes run **~1.4× the 160-word target**. The cited URLs are no longer
  appended — they were 48% of everything written and the Email agent, the
  field's only reader, cannot use them; they stay on the `model_response` log
  line. `lead_context` now lands at ~1.5-2.3k chars of prose.
- `industry` / `limit` on the campaign route are **unreachable from the
  gateway** — absent from `ALLOWED_METADATA_KEYS`, and the gateway *raises* on
  an extra key. Hand-driven use only.
- The gateway still lists `blog_published_at` in `ALLOWED_METADATA_KEYS` with a
  stale comment claiming the MCP reads by it. Harmless — `dispatch.py` stopped
  sending it — but the rationale is wrong.

## 10. Working agreements

- Never push or open a PR without explicit confirmation.
- Secrets come from Secret Manager. `.env` is git-ignored and now holds no
  credential; `.env.bak` is ignored too (`setup_env.sh` creates it).
- Bad records are flagged with a reason, never silently dropped.
- Config is env-driven; a rename outside is a config change, never a code edit.
- Verify by running, not by reading. Several bugs above were found only because
  a documented command was actually executed.
