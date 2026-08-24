# Research Agent — setting up a new machine

From nothing to a working campaign. Six steps, ~10 minutes.

You need: **Python 3.11+**, **Docker**, **gcloud**, and read access to Secret
Manager in `ldqfingsrv-dev` (`roles/secretmanager.secretAccessor`). No keys are
emailed or pasted — everything comes from Secret Manager.

---

## 1. Get the code

```bash
git clone <repo-url> LQABR
cd LQABR
git checkout leadq-dev-rao
```

## 2. Python environment

The agent is **standalone** — it imports nothing from `packages/lqabr_core` and
nothing from another agent, so this is the only install you need.

```bash
python3 -m venv ~/lqabr-venv
source ~/lqabr-venv/bin/activate
pip install -r agents/research/requirements.txt
```

## 3. Authenticate to Google

Two separate logins. Both are needed: the first is for the `gcloud` CLI, the
second writes the credentials file the MCP container mounts.

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project ldqfingsrv-dev
```

Check it worked:

```bash
gcloud secrets versions access latest \
  --secret=lqabr-anthropic-api-key --project=ldqfingsrv-dev | wc -c
```

A number (~108) means you have access. An error means ask an admin for the
`secretAccessor` role before going further.

## 4. Start the MCP container

**Every** HubSpot read and write goes through this. The agent will start
without it (`mcp_startup_check: warn`) but every run then fails at the first
read, so start it first.

```bash
docker run -d --name lqabr-mcp-gcp -p 8080:8080 \
  -v "$HOME/.config/gcloud/application_default_credentials.json:/gcp/adc.json:ro" \
  -e GOOGLE_APPLICATION_CREDENTIALS=/gcp/adc.json \
  -e LQABR_SECRET_PROJECT=ldqfingsrv-dev \
  -e LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN=lqabr-hubspot-access-token \
  -e HUBSPOT_AUTH_MODE=private_app \
  -e MCP_TRANSPORT=streamable-http \
  -e MCP_HOST=0.0.0.0 -e PORT=8080 -e MCP_PATH=/mcp \
  tne736/lqabr-mcp-server:latest
```

Those two `LQABR_SECRET_*` values are secret **names**, not secrets — the
container resolves them itself at run time.

```bash
docker ps --filter name=lqabr-mcp-gcp     # expect "Up"
```

## 5. Write the .env

```bash
cd agents/research
./setup_env.sh              # writes are OFF — safe first run
```

It pulls the model key from Secret Manager and writes `.env` (git-ignored).
The HubSpot token is deliberately **not** written: the agent resolves it by
name at run time, so it never lands on disk.

When you actually want to write to HubSpot:

```bash
./setup_env.sh --live
```

## 6. Run it

```bash
cd agents/research
set -a && source .env && set +a
python -m uvicorn service_app:app --port 8086 --app-dir src
```

**Source `.env` first.** Exporting a variable and *then* sourcing `.env`
overwrites it — that is how an empty `ANTHROPIC_API_KEY` silently killed a
whole run once.

Give it ~10 seconds: it imports the SDK and discovers the MCP tools before it
answers. A health check earlier than that reports a healthy agent as down.

---

## Verify

```bash
curl -s localhost:8086/health    | python3 -m json.tool   # mcp.reachable: true
curl -s localhost:8086/mcp/tools | python3 -m json.tool   # missing: []
```

In `/health`, check `dry_run` matches what you intended. In the startup log,
`mcp_startup_check_ok` with 4 tools means the container is wired correctly.

## Run a campaign

One post becomes `lead_context` for every lead in its industry:

```bash
curl -s -X POST localhost:8086/research/campaign \
  -H 'Content-Type: application/json' \
  -d '{"object_id":"330008697562"}' | python3 -m json.tool
```

`object_id` is the **blog post's** record id. Expect `leads_found`, `written`,
`failed`, `skipped`, and a row per lead. Watch the console: it prints
`lead 3/5 ... (2 left)` as it goes. Roughly 30s per lead.

One lead only:

```bash
curl -s -X POST localhost:8086/research/run \
  -H 'Content-Type: application/json' \
  -d '{"object_id":"533970643697","summary_ref_id":"330008697562"}'
```

Here `object_id` is the **contact** and `summary_ref_id` is the post. They are
different records — swapping them reads the wrong row.

## Tests — no credentials, no network

```bash
cd agents/research
PYTHONPATH=src:packages python -m pytest -q
```

---

## When it does not work

| What you see | What it is |
|---|---|
| `ANTHROPIC_API_KEY is not set` on every lead | `.env` was sourced *after* the key was exported, so the blank overwrote it. Source `.env` first. |
| Everything succeeds but HubSpot is unchanged | `LQABR_RESEARCH_DRY_RUN=1`. Check `dry_run` in `/health`. |
| `address already in use` on 8086 | An older instance is still up: `ss -ltnp \| grep 8086`, then kill that pid. |
| `POST /mcp 404` | Port 8086 is the *agent*; the MCP is 8080. Check `mcp_base_url` in `/health`. |
| MCP calls fail after re-running `gcloud auth application-default login` | A single-file bind mount keeps the old inode. `docker restart lqabr-mcp-gcp`. |
| `Invalid leading whitespace ... in header value` | A `\r` from CRLF line endings in `.env`: `sed -i 's/\r$//' .env`. |
| `search_kwargs_dropped  dropped=[temperature]` | Expected. Anthropic SDK 1.0.0 removed `temperature`; the call proceeds without it. |
| `run_failed step=list_leads` | The industry lookup could not reach HubSpot. The campaign aborts on purpose rather than reporting an empty run that skipped every lead. |

## Reading the logs

One readable line per event; the log file stays JSON.

```
13:10:23 ✓ campaign_leads_found     industry=FINANCIAL_SERVICES leads_found=5
13:10:23 · lead 1/5    533970643697  working…
13:10:47 → anthropic POST messages.create 200 20738ms
13:10:50 ✓ lead 1/5    533970643697 completed chars=3557  (4 left)
```

`·` step · `✓` succeeded · `!` degraded but continuing · `✗` failed ·
`→` an outbound call.

Set `LQABR_RESEARCH_LOG_FORMAT=json` to force JSON on a terminal. The file at
`logs/agents/research/agent.log` is always JSON, whatever the console shows.

---

See also: `RUNBOOK.md` (day-to-day), `API.md` (the HTTP contract),
`ENV_VARS.md` (every knob), `DESIGN.md` (why each piece exists).
