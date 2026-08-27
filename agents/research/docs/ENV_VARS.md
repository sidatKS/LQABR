# Research Agent — environment variables

Precedence: **environment > `config/config.yaml` > code default.**
Leave a variable unset to take the config map. Names only in `.env.example`.

## Model

| Variable | Default | Notes |
| --- | --- | --- |
| `LQABR_RESEARCH_MODEL` | `claude-sonnet-4-6` | an `anthropic/`-prefixed value is accepted; the prefix is stripped |
| `LQABR_RESEARCH_MAX_TOKENS` | `2000` | |
| `ANTHROPIC_API_KEY` | — | Optional override. Normally unset: the key is read from Secret Manager as `LQABR_RESEARCH_MODEL_TOKEN_SECRET`. When set, it wins and the use is logged. |
| `LQABR_RESEARCH_MODEL_TOKEN_SECRET` | `lqabr-anthropic-api-key` | The model credential's NAME in Secret Manager — never its value. |

## MCP — the only door to HubSpot

| Variable | Default | Notes |
| --- | --- | --- |
| `LQABR_RESEARCH_MCP_BASE_URL` | `http://localhost:8091/mcp` | |
| `LQABR_RESEARCH_MCP_PROTOCOL_VERSION` | `2025-06-18` | |
| `LQABR_RESEARCH_MCP_AUTH_TOKEN` | *(empty)* | bearer, when the MCP requires one |
| `LQABR_RESEARCH_MCP_TIMEOUT_SECONDS` | `60` | the central upsert can take ~25s |
| `LQABR_RESEARCH_MAX_RETRIES` | `3` | total attempts |
| `LQABR_RESEARCH_MCP_BACKOFF_BASE_SECONDS` | `1.0` | delay = base · 2^(n−1) |
| `LQABR_RESEARCH_MCP_BACKOFF_CAP_SECONDS` | `8.0` | |
| `LQABR_RESEARCH_MCP_RETRYABLE_STATUSES` | `429,500,502,503,504` | 404 is always a session re-init |
| `LQABR_RESEARCH_MCP_OBJECT_ID_ARG` | `objectId` | what the MCP calls the record id in its tool arguments |
| `LQABR_RESEARCH_MCP_TOOL_READ_LEAD` | `get_lead_profile` | |
| `LQABR_RESEARCH_MCP_TOOL_READ_BLOG` | `get_blog_summary` | keyed on `blog_published_at` |
| `LQABR_RESEARCH_MCP_TOOL_WRITE` | `upsert_lead_profile` | |
| `LQABR_RESEARCH_MCP_ASSERT_TOOLS` | `1` | `0` = start even if a tool is missing |
| `LQABR_RESEARCH_MCP_STARTUP_CHECK` | `warn` | `warn` \| `strict` \| `off` |

## HubSpot target

| Variable | Default | Notes |
| --- | --- | --- |
| `LQABR_RESEARCH_HUBSPOT_CONTEXT_PROPERTY` | `lead_context` | the gateway's R2 route watches this |
| `LQABR_RESEARCH_DRY_RUN` | `0` | `1` = compute and log, never send |
| `LQABR_RESEARCH_SKIP_IF_CONTEXT_PRESENT` | `0` | `1` = leave a lead that already has a note |

## Web search

| Variable | Default | Notes |
| --- | --- | --- |
| `LQABR_RESEARCH_SEARCH_ENABLED` | `1` | |
| `LQABR_RESEARCH_SEARCH_MAX_USES` | `5` | searches per pass |
| `LQABR_RESEARCH_SEARCH_TIMEOUT_SECONDS` | `90` | |
| `LQABR_RESEARCH_SEARCH_ALLOWED_DOMAINS` | *(empty)* | comma-separated; **set one, not both** |
| `LQABR_RESEARCH_SEARCH_BLOCKED_DOMAINS` | *(empty)* | |
| `LQABR_RESEARCH_SEARCH_TOOL_TYPE` | `web_search_20250305` | the server-tool id, if it moves |

## Note shape

| Variable | Default |
| --- | --- |
| `LQABR_RESEARCH_NOTE_TARGET_WORDS` | `160` |
| `LQABR_RESEARCH_NOTE_MAX_CHARS` | `60000` (HubSpot caps at 65 536) |

## Service

| Variable | Default |
| --- | --- |
| `PORT` | `8086` |
| `LQABR_RESEARCH_ROUTE_A2A` | `/research/a2a` |
| `LQABR_RESEARCH_ROUTE_RUN` | `/research/run` |
| `LQABR_RESEARCH_CORS_ORIGINS` | `http://localhost:5173` |

## Secrets + logging

| Variable | Default |
| --- | --- |
| `LQABR_RESEARCH_SECRETS_SOURCE` | `env` (`env` \| `secret_manager` \| `auto`) |
| `LQABR_RESEARCH_GCP_PROJECT` | *(empty)* |
| `LQABR_RESEARCH_LOG_LEVEL` | `INFO` |
| `LQABR_RESEARCH_LOG_DIR` | `logs/research` — where the three per-stream files go (relative → repo root; absolute honoured; **empty disables file logging**) |
| `LQABR_RESEARCH_LOG_MODE` | `normal` (`terse` \| `normal` \| `debug`) — how much of a value reaches the log. See the caution below |
| `LQABR_RESEARCH_LOG_FORMAT` | `auto` (`auto` \| `text` \| `json`) — console shape only; the FILES are always JSON |
| `LQABR_RESEARCH_LOG_MAX_BYTES` | `52428800` — 50 MB before a stream's file rolls over; `0` = never |
| `LQABR_RESEARCH_LOG_BACKUPS` | `5` — `research_process.log.1` … `.5`; the live file keeps its exact name |
| `LQABR_RESEARCH_LOG_FILE` | *(unset)* — **deprecated.** Set it and all three streams share one file; the boot emits `log_sink_legacy` naming it |
| `LQABR_RESEARCH_LOG_DETAIL` | *(unset)* — **deprecated alias.** `0` → `terse`, `1` → `normal`, and the boot emits `log_detail_deprecated` |

### The three log files

One file per stream, under `LQABR_RESEARCH_LOG_DIR`:

| File | Holds |
|---|---|
| `research_process.log` | the steps — `step_in` / `step_out`, decisions, counts |
| `research_audit.log` | every hop that left the process, with the parameters it was made with and what it **cost** (`input_tokens`, `output_tokens`, `web_search_requests`) |
| `research_system.log` | boot, config and shutdown |

A run spans all three; `run_id` is on every record. See RUNBOOK.md for the grep
that reassembles one.

### `debug` mode — read this before using it

`debug` does not add logging. It stops values being **mangled on the way in**:
every truncation happens before `json.dumps`, so the structured file has been
storing damaged strings in well-formed fields. In debug the full prompt, the
full note and the full call arguments reach the file, whole.

* Credentials are **still redacted**, in every mode. `<redacted>` is a
  substitution, not a truncation.
* But `redact()`'s 500-character trim is lifted, and that trim was also an
  incidental cap on how much of a credential could escape had one arrived as a
  *value* under an innocent field name. Name-based redaction is the only net
  left. **Do not run debug mode on a shared box.**
* `logs/` and `*.log` are git-ignored and excluded from both Docker builds.
| `LQABR_RESEARCH_CONFIG_FILE` | `<agent>/config/config.yaml` |
