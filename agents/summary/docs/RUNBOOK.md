# Summary Agent — runbook

## Local, no cloud, no CRM

```bash
cd agents/summary
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                # add ANTHROPIC_API_KEY

python3 -m pytest -c tests/pytest.ini -q             # offline, no key needed
```

Summarise something without touching HubSpot:

```bash
cd src
python3 agent.py --url https://spring.io/blog
python3 agent.py --text "some prose"
python3 agent.py --json-file ../sample.json --select '$.data.article'
python3 agent.py --endpoint http://localhost:9000/report --method GET
```

Serve it:

```bash
uvicorn service_app:app --port 8080 --app-dir src   # from agents/summary
curl localhost:8080/health
curl -sX POST localhost:8080/summary/run -H 'Content-Type: application/json' \
     -d '{"source": "https://spring.io/blog"}' | jq .
```

ADK surfaces:

```bash
adk web agents/summary/src
adk run agents/summary/src
adk api_server agents/summary/src
```

## Local, against the HubSpot MCP container

```bash
cd agents/summary
HUBSPOT_MCP_IMAGE=<your-image> ANTHROPIC_API_KEY=… \
  docker compose -f infra/docker-compose.yml up --build

curl localhost:8080/mcp/tools | jq .      # ← confirms the tool names
```

`/mcp/tools` is the first thing to check, always. `missing: []` means the
configured names match the server. If it lists names, point
`LQABR_SUMMARY_MCP_TOOL_READ` / `_WRITE` / `_LIST_LEADS` at the real ones —
no code change, no rebuild.

Compose defaults to `LQABR_SUMMARY_DRY_RUN=1`: the write is computed and
logged, and nothing reaches the CRM. Do a dry run first, read the
`hubspot_write_dry_run` log line, then set `LQABR_SUMMARY_DRY_RUN=0`.

## Deploy

```bash
cd agents/summary
source infra/config.sh
./infra/01_secrets.sh          # once — grant the runtime SA the model key
./infra/02_build_push.sh
./infra/03_deploy_run.sh       # deploys with DRY_RUN=1 by default
```

Then, in order:

1. `GET /health` → `mcp.reachable`, `mcp.tools`, `hubspot.summary_property`
2. `GET /mcp/tools` → `missing: []`
3. one `POST /summary/run` with an `object_id`, still dry → read the response's
   `hubspot.status: "dry_run"` and its `properties`
4. set `LQABR_SUMMARY_DRY_RUN=0`, redeploy, repeat step 3 → `"written"`
5. confirm the property in HubSpot, and that the gateway's `R-blog-summary`
   route fired if you are routing through it

## Reading a failure

Every response carries a `run_id`; every log line carries the same one.

| Symptom | Where to look |
|---|---|
| `status: failed`, `error` mentions a host or scheme | the fetch guard refused the source. Allowlist it or fix the URL. |
| `status: failed`, "did not return a usable summary" | the model answered twice without valid JSON. The raw answer is on the `model_output_invalid` log line. |
| `hubspot.status: error`, "HTTP 5xx" | the MCP container. Check it is up and `/mcp/tools` answers. |
| `hubspot.status: error`, "bad-data: …" | the MCP validated and refused the write — usually a property name the portal does not have. |
| `hubspot.status: skipped` | no `object_id` was supplied; the summary was returned, nothing was written. |
| `/health` shows `mcp.reachable: false` | the MCP was asleep or unreachable at boot. Not fatal in `warn` mode; `/mcp/tools` re-checks live. |

## Rollback

```bash
gcloud run services update-traffic lqabr-summary-agent \
  --region "$REGION" --to-revisions <previous-revision>=100
```

Nothing this agent does is stateful; a rollback is immediate. A summary
already written to HubSpot stays written — clear the property in the portal
if a bad summary needs undoing.
