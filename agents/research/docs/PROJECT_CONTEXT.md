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
   -> Agent Gateway routes it (hands over ONLY the post's object_id)
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

**The two a2a routes differ by what the id IS.** On `/research/a2a` the
`object_id` is a CONTACT; on `/research/campaign/a2a` it is a blog POST. Sending
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
| `POST /research/run` silently ignored `summary_ref_id` | `resolved()` read it only from the nested target | mirrored like `object_id` |
| The CLI could never succeed | required `--blog-published-at`, which stopped being the blog key | `--summary-ref-id` |
| CLI output unparseable | logs and the result JSON on the same stream | logs to stderr; stdout is the result document |
| `2 validation errors for call[get_blog_summary]` | the MCP container is an **older image** keyed on `blog_published_at` | `docker pull` and recreate the container |
| Log lines corrupting mid-run | a line longer than the terminal wrapped and redrew over its neighbour | measure against real terminal width; diagnostics continue on indented lines |
| `bash\r: No such file` | CRLF line endings on a `.sh` | `.gitattributes` pins `*.sh` to LF; never write these files from Windows text mode |

## 6. Observability

Three JSON streams to stdout, one `run_id` per campaign:

- **process** — the steps: `started` / `ok` / `failed` / `skipped` / `degraded`
- **audit** — every outbound call: service, endpoint, status, attempt, ms, and
  the credential's **name**, never its value
- **system** — startup and config

`LQABR_RESEARCH_LOG_FORMAT` = `auto` (default) / `text` / `json`. **auto** means
readable text when stdout is a terminal, JSON when it is not — so Cloud Run
keeps its structured fields and a human gets something legible. **The log file
is always JSON.**

```
13:10:23 ✓ campaign_leads_found     industry=FINANCIAL_SERVICES leads_found=5
13:10:23 · lead 1/5    533970643697  working…
13:10:47 → anthropic POST messages.create 200 20738ms
13:10:50 ✓ lead 1/5    533970643697 completed chars=3557  (4 left)
```

Rules the tests enforce: secrets stay redacted in the readable form too; no line
exceeds the terminal width; a failure `reason` is never truncated away.

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
- Notes run **~2× the 160-word target** and carry ~25 appended source URLs, so
  `lead_context` lands at 3.5–4k chars. That is what the Email agent reads.
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
