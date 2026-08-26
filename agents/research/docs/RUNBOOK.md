# Research Agent — runbook

## Start

```bash
cd <repo root>
source .venv/bin/activate
uvicorn service_app:app --port 8086 --app-dir agents/research/src
```

Give it ~10 seconds: it imports the SDK and runs MCP tool discovery before it
answers. A health check earlier than that reports "down" on an agent that is
merely still booting.

Prerequisite: the **HubSpot MCP container must be up**. In practice it runs on
`:8080` (`tne736/lqabr-mcp-server:latest`) — note the code *default* is `:8091`,
so the URL is set explicitly in `.env`. `setup_env.sh` writes `:8080`; if you
build `.env` by hand, check `mcp.url` in `/health` matches where the container
actually is. The agent starts without it — `mcp_startup_check: warn` — but
every run then fails at the first read.

Starting the container from scratch: see `SETUP.md`.

## Verify

```bash
curl -s localhost:8086/health   | python3 -m json.tool     # mcp.reachable: true
curl -s localhost:8086/mcp/tools | python3 -m json.tool     # missing: []
```

Then a dry run — computes the note, logs the write, sends nothing:

```bash
LQABR_RESEARCH_DRY_RUN=1 python3 agents/research/src/agent.py \
  --object-id <CONTACT id> --summary-object-id <BLOG POST id>
```

`/health` also reports where the logs are going:

```json
"logging": {"mode": "normal",
            "dir": "logs/research",
            "files": {"process": "research_process.log",
                      "audit": "research_audit.log",
                      "system": "research_system.log"},
            "degraded": []}
```

`degraded` is the one field to read. It is empty on a healthy agent; an entry
such as `process:open` or `audit:rotate` means that stream fell back to the
console and its file is **not** being written. The run itself is unaffected —
observability degrades, it never stops a campaign.

Then live — the campaign route is the only one the gateway drives, and its
`objectId` is a blog **POST**:

```bash
curl -sX POST localhost:8086/research/campaign/a2a \
  -H 'Content-Type: application/json' \
  -d '{"params":{"metadata":{"objectId":"<blog post id>",
                             "subscriptionType":"ticket.propertyChange"}}}' \
  | python3 -m json.tool
```

It acks immediately and runs in the background; the outcome is in the logs, not
in that response. For one lead by hand, use the CLI — it needs both ids:

```bash
python3 agents/research/src/agent.py \
  --object-id <CONTACT id> --summary-object-id <BLOG POST id>
```

## Read the logs

Three files, one directory (`LQABR_RESEARCH_LOG_DIR`, default `logs/research`):

| File | Stream | Holds |
| --- | --- | --- |
| `research_process.log` | process | what the run did — steps in and out, their inputs, their outputs, the decisions |
| `research_audit.log` | audit | every outbound hop: service, endpoint, status, duration, attempt, and what the call **cost** in tokens |
| `research_system.log` | system | boot, resolved config, sink state, shutdown |

The split is deliberate: *audit records what a call cost, process records what
it produced*. Token counts therefore ride the audit hop and are **not** in the
process log.

```bash
tail -f logs/research/research_process.log

# the outcome of the last run
grep -E '"event":"(run_complete|run_failed|campaign_complete)' \
  logs/research/research_process.log | tail -3

# every step of the last run, with its inputs, outputs and duration
grep '"event":"step_' logs/research/research_process.log | tail -20

# what every outbound call cost
grep '"event":"outbound_call"' logs/research/research_audit.log | tail -10
```

### One run, across all three files

Every line of a run carries the same `run_id`. That is the join key — and it is
the gateway's own id when the gateway dispatched the run, which ties the two
services' logs together too:

```bash
grep -hE '"run_id": ?"res-abc123def456"' logs/research/*.log | jq -s 'sort_by(.ts)'
```

The `?` matters: `json.dumps` emits `"run_id": "…"` **with** a space, so a
pattern without one silently matches nothing. `grep -h` drops the filename
prefix so the output stays parseable JSON;
`jq -s` slurps it and `sort_by(.ts)` interleaves the three streams back into
the order things actually happened.

### Detail modes

`LQABR_RESEARCH_LOG_MODE` — `terse` | `normal` | `debug` (default `normal`).

| Mode | What changes |
| --- | --- |
| `terse` | outbound hops carry no `params`; previews are dropped. Smallest files. |
| `normal` | params and length-marked previews. What you want day to day. |
| `debug` | nothing is trimmed: full prompt, full model answer, full note, full params, every field on its own console line. |

### What debug adds to a model call

In `normal` the system prompt is logged **once** — in full the first time it is
used, and again only if it changes; after that `system_chars` is the whole
story, because it is the same 1,500 characters on every lead. The user prompt
is always logged in full.

In `debug` two more things appear on every `model_request`:

| Field | What it holds |
| --- | --- |
| `system_preview` | the full system prompt on **every** request, not just the first — no scrolling back through the run to find it |
| `payload` | the exact dict handed to the SDK: `model`, `max_tokens`, `messages`, `system`, `tools`. `sent_keys` names the keys; this says what was in them. |

```bash
# the complete prompt for one lead's model call
grep '"event": "model_request"' logs/research/research_process.log \
  | jq -r 'select(.objectId=="<contact id>") // . | .payload.system, .payload.messages[0].content'
```

`payload` is redacted like any other field, so a credential's value never
reaches it — but it does hold the whole prompt twice over, which is one more
reason debug is not a default.

> **Caution — `debug` is not a "more logging" switch.**
> It lifts `redact()`'s 500-character trim, and that trim was also an
> incidental cap on how much of a credential could escape had one arrived as a
> value under an innocent field name. Name-based redaction is the only net
> left. The process log in debug mode also holds the full prompt and the full
> generated note. **Do not run debug mode on a shared box**, and do not leave
> it on after the question it was turned on to answer has been answered.
> `logs/` and `*.log` are in `.dockerignore` for the same reason.
>
> A credential's **name** is printed in every mode, including debug — that is
> the rule, not an oversight. `secret_name: hubspot-private-app-token` is a
> name; a token count is not a token.

## Failures

| Symptom | Cause | Fix |
| --- | --- | --- |
| `/health` → `mcp.reachable: false` | the MCP container is down | `bash mcp/run.sh` |
| `/mcp/tools` → `missing: [...]` | tool renamed on the server | point `LQABR_RESEARCH_MCP_TOOL_*` at the real names — config, not code |
| `crm-error: the MCP returned no lead` | wrong contact id, or the MCP cannot read HubSpot | check `logs/mcp/hubspot.log` for `secret_manager_access` |
| `crm-error: no blog summary found` | no post at that timestamp | confirm the value against the ticket; the MCP reads by timestamp, not ticket id |
| `ANTHROPIC_API_KEY is not set` | key missing from `.env` | set it; it powers both the model and the search |
| write `status: halted` in the reason | the **MCP's** GCP creds expired | `bash mcp/reauth.sh` — ADC expires hourly and the container pins the old file |
| `bad-data: lead is missing [...]` | the contact has no `employee_id` / `company_id` / `decision_maker_flag` | the write tool requires all three; fix the lead profile upstream |

## The chain

A successful write raises HubSpot trigger 2. To confirm the chain rather than
just the agent:

```bash
# 1. the write landed
grep '"event":"context_write_ok"' logs/research/research_process.log | tail -1

# 2. HubSpot delivered the resulting event to the gateway
grep lead_context logs/gateway/gateway.jsonl | tail -3
```

If step 1 is present and step 2 is empty, the agent did its job and the problem
is the HubSpot subscription on `lead_context` or the gateway's target URL — not
this agent.

Note: HubSpot raises `propertyChange` only when the value actually **changes**.
Re-running research on a lead whose note is byte-identical produces no event.
The note carries citations and a fresh research pass, so this is rare in
practice — but it explains a "the chain stopped" report after a repeat run.

## Restart after a config change

`.env` and `config/config.yaml` are read **once, at boot**. Restart after any
edit:

```bash
fuser -k 8086/tcp; sleep 2
nohup uvicorn service_app:app --port 8086 --app-dir agents/research/src >/dev/null 2>&1 &
```
