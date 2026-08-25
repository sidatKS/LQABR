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
| `LQABR_RESEARCH_INCLUDE_SOURCES` | `1` |

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
| `LQABR_RESEARCH_LOG_FILE` | `logs/agents/research/agent.log` (relative → repo root; empty disables) |
| `LQABR_RESEARCH_CONFIG_FILE` | `<agent>/config/config.yaml` |
