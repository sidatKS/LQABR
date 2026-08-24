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
LQABR_RESEARCH_DRY_RUN=1 python agents/research/src/agent.py \
  --object-id <contact id> --blog-published-at <timestamp>
```

Then live:

```bash
curl -sX POST localhost:8086/research/run -H 'Content-Type: application/json' \
  -d '{"object_id":"<contact id>","blog_published_at":"<timestamp>"}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], "|", d["hubspot"]["status"], "|", d["error"][:120])'
```

## Read the logs

```bash
tail -f logs/agents/research/agent.log

# just the outcome of the last run
grep -E '"event":"(run_complete|run_failed|context_write_)' logs/agents/research/agent.log | tail -3
```

Streams: `process` (what it did and why), `audit` (every outbound hop —
endpoint, status, duration; never a payload), `system` (start/stop).
Correlate a run with `run_id`; when the gateway dispatched it, that id is the
gateway's own, which ties the two logs together.

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
grep '"event":"context_write_ok"' logs/agents/research/agent.log | tail -1

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
