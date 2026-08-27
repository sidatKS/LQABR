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

### Three files, one directory

`LQABR_SUMMARY_LOG_DIR` (default `logs/summary`):

| File | Stream | Holds |
| --- | --- | --- |
| `summary_process.log` | process | what the run did — steps in and out, their inputs, their outputs, the decisions |
| `summary_audit.log` | audit | every outbound hop: service, endpoint, status, duration, attempt, and what the call **cost** in tokens |
| `summary_system.log` | system | boot, resolved config, sink state, shutdown |

*Audit records what a call cost; process records what it produced.* Token
counts ride the audit hop and are **not** in the process log.

`GET /health` reports the sink state under `logging`:

```json
"logging": {"mode": "normal",
            "dir": "logs/summary",
            "files": {"process": "summary_process.log",
                      "audit": "summary_audit.log",
                      "system": "summary_system.log"},
            "degraded": []}
```

`degraded` is empty on a healthy agent. An entry such as `process:open` or
`audit:rotate` means that stream fell back to the console and its file is not
being written — the run itself is unaffected, because observability degrades
rather than stopping work.

### One run, across all three files

`run_id` is the join key:

```bash
grep -hE '"run_id": ?"sum-abc123def456"' logs/summary/*.log | jq -s 'sort_by(.ts)'
```

The `?` matters: `json.dumps` emits `"run_id": "…"` **with** a space, so a
pattern without one silently matches nothing. `grep -h` drops the filename
prefix so the output stays parseable JSON; `jq -s`
slurps it and `sort_by(.ts)` interleaves the three streams back into the order
things actually happened. When the gateway dispatched the run, that `run_id` is
the gateway's own — the same grep against `logs/gateway/` joins the two
services.

### Detail modes

`LQABR_SUMMARY_LOG_MODE` — `terse` | `normal` | `debug` (default `normal`).

| Mode | What changes |
| --- | --- |
| `terse` | outbound hops carry no `params`; previews are dropped. Smallest files. |
| `normal` | params and length-marked previews. What you want day to day. |
| `debug` | nothing is trimmed: full prompt, full model answer, full summary, full params, every field on its own console line. |

`--debug` on the CLI sets the same mode for that one invocation. Diagnostics go
to the log handlers, not to stdout, so `python3 agent.py --url … --debug | jq .`
still parses.

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
grep '"event": "model_request"' logs/summary/summary_process.log \
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
> generated summary. **Do not run debug mode on a shared box**, and turn it off
> once the question it was turned on to answer has been answered. `logs/` and
> `*.log` are in `.dockerignore` for the same reason.
>
> A credential's **name** is printed in every mode, debug included — that is
> the rule, not an oversight. `secret_name: anthropic-api-key` is a name; a
> token count is not a token.


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
