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

It writes `.env` (git-ignored) with **no credential in it**. Both the model key
and the HubSpot token are resolved from Secret Manager at run time, by the names
in that file — so nothing secret lands on disk, and a key rotation needs no
change on anyone's machine. What the script does check is that your identity can
actually *read* both secrets, which is far better found now than as every lead
failing later.

This means valid ADC is required to run, not just to set up. If the research
step starts failing with `no model credential`, re-run
`gcloud auth application-default login`.

When you actually want to write to HubSpot:

```bash
./setup_env.sh --live
```

## 6. Run it

```bash
cd agents/research
source ~/lqabr-venv/bin/activate
set -a && source .env && set +a
python3 -m uvicorn service_app:app --port 8086 --app-dir src
```

**Source `.env` first.** Exporting a variable and *then* sourcing `.env`
overwrites it — that is how an empty `ANTHROPIC_API_KEY` silently killed a
whole run once. (It no longer carries credentials, but the ordering trap
applies to every variable in it.)

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
  -d '{"objectId":"330008697562"}' | python3 -m json.tool
```

`objectId` is the **blog post's** record id. Expect `leads_found`, `written`,
`failed`, `skipped`, and a row per lead. Watch the console: it prints
`lead 3/5 ... (2 left)` as it goes. Roughly 30s per lead.

One lead only:

```bash
curl -s -X POST localhost:8086/research/run \
  -H 'Content-Type: application/json' \
  -d '{"objectId":"533970643697","summary_objectId":"330008697562"}'
```

Here `objectId` is the **contact** and `summary_objectId` is the post. They are
different records — swapping them reads the wrong row.

## Tests — no credentials, no network

```bash
cd agents/research
source ~/lqabr-venv/bin/activate
PYTHONPATH=src:packages python3 -m pytest -q
```

---

## When it does not work

| What you see | What it is |
|---|---|
| `no model credential` on every lead | ADC expired, or this identity cannot read `lqabr-anthropic-api-key`. Run `gcloud auth application-default login`. |
| Everything succeeds but HubSpot is unchanged | `LQABR_RESEARCH_DRY_RUN=1`. Check `dry_run` in `/health`. |
| `address already in use` on 8086 | An older instance is still up: `ss -ltnp \| grep 8086`, then kill that pid. |
| `POST /mcp 404` | Port 8086 is the *agent*; the MCP is 8080. Check `mcp_base_url` in `/health`. |
| MCP calls fail after re-running `gcloud auth application-default login` | A single-file bind mount keeps the old inode. `docker restart lqabr-mcp-gcp`. |
| `Invalid leading whitespace ... in header value` | A `\r` from CRLF line endings in `.env`: `sed -i 's/\r$//' .env`. |
| `search_kwargs_dropped  dropped=[temperature]` | Expected. Anthropic SDK 1.0.0 removed `temperature`; the call proceeds without it. |
| `run_failed step=list_leads` | The industry lookup could not reach HubSpot. The campaign aborts on purpose rather than reporting an empty run that skipped every lead. |

## Reading the logs

One readable line per event, and every step framed by an IN/OUT pair: what it
was handed, what it produced, how long it took. The log file stays JSON.

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

`LQABR_RESEARCH_LOG_DETAIL=0` drops the previews and the parameter bags if you
want the terser shape.

Set `LQABR_RESEARCH_LOG_FORMAT=json` to force JSON on a terminal. The file at
`logs/agents/research/agent.log` is always JSON, whatever the console shows.

---

## Appendix: WSL (Windows)

The reference machine is **Ubuntu 24.04 under WSL2**, with Docker installed
*inside* WSL — not Docker Desktop. Everything above still applies; these are
the differences that will bite you.

### Install WSL, then work inside it

From **PowerShell as Administrator**, once:

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot, set a username and password, then open **Ubuntu** — not PowerShell.
Every command in this guide runs in the Ubuntu shell.

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip docker.io git
sudo usermod -aG docker "$USER"      # then close and reopen the shell
```

There is no systemd here, so start the Docker daemon per session:

```bash
sudo service docker start
service docker status                # expect "active"
```

### 1. gcloud is not on PATH in a non-login shell

The SDK installs to `~/google-cloud-sdk` and is wired up by `.bashrc`. A script,
a cron job, or a `wsl -e bash -c` invocation gets neither and fails with
`gcloud: command not found`.

```bash
source ~/google-cloud-sdk/path.bash.inc
```

Put that line at the top of anything that shells out to `gcloud`.

### 2. Where you clone matters

| Location | Trade-off |
|---|---|
| `/mnt/c/Users/<you>/LQABR` | Visible to Windows editors. **Slow** file I/O, and `chmod` does nothing. |
| `~/LQABR` | Much faster, real Unix permissions. Reachable from Windows Explorer via the `\\wsl$` share. |

Either works. Prefer `~` unless Windows tools need to edit the files directly.

### 3. chmod is a no-op on /mnt/c

DrvFs ignores permissions, so `.env` stays world-readable however you chmod it.
`setup_env.sh` detects this and reports the mode it actually got rather than
claiming one it did not. The file is git-ignored, but it holds a live API key —
on a shared machine, clone to `~` instead.

### 4. CRLF line endings break credentials

A `.env` touched by a Windows editor gets CRLF. The carriage return rides along
inside the value and the HTTP layer rejects it:

```
Invalid leading whitespace, reserved character(s), or ... in header value
```

```bash
sed -i 's/\r$//' .env
```

`setup_env.sh` always writes LF, so this only happens to hand-edited files.

### The venv belongs on the Linux side

Even with the repo on `/mnt/c`, keep the virtualenv in the Linux filesystem —
`~/lqabr-venv`, not inside the repo. A venv on DrvFs is slow and its activation
scripts carry the wrong paths.

```bash
python3 -m venv ~/lqabr-venv
source ~/lqabr-venv/bin/activate
pip install -r agents/research/requirements.txt
```

### One block, every session

```bash
source ~/google-cloud-sdk/path.bash.inc
source ~/lqabr-venv/bin/activate
sudo service docker start
docker start lqabr-mcp-gcp 2>/dev/null || true

cd /mnt/c/Users/<you>/LQABR/agents/research
set -a && source .env && set +a
python3 -m uvicorn service_app:app --port 8086 --app-dir src
```

### Reaching it from Windows

`localhost:8086` works from a Windows browser — WSL2 forwards it. The reverse
(WSL reaching a service running on Windows) needs the host IP, not `localhost`.

---

See also: `RUNBOOK.md` (day-to-day), `API.md` (the HTTP contract),
`ENV_VARS.md` (every knob), `DESIGN.md` (why each piece exists).
