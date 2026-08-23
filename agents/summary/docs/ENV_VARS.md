# Summary Agent — environment variables

Every name below has a default. Renaming an MCP tool or a HubSpot property is
a config change here, never a code edit. `.env.example` is the copyable
version of this table.

## Model

| Variable | Default | Notes |
|---|---|---|
| `LQABR_SUMMARY_MODEL` | `anthropic/claude-sonnet-5` | LiteLLM model id |
| `LQABR_SUMMARY_TEMPERATURE` | `1.0` | |
| `ANTHROPIC_API_KEY` | — | local dev; prod resolves it from Secret Manager |

## MCP — the runtime connection

| Variable | Default | Notes |
|---|---|---|
| `LQABR_SUMMARY_MCP_BASE_URL` | `http://localhost:8080/mcp` | the HubSpot MCP container. **The only coupling.** |
| `LQABR_SUMMARY_MCP_TIMEOUT_SECONDS` | `30` | |
| `LQABR_SUMMARY_MCP_AUTH_TOKEN` | — | sent as `Authorization: Bearer …` |
| `LQABR_SUMMARY_MCP_PROTOCOL_VERSION` | `2025-06-18` | |
| `LQABR_SUMMARY_MCP_TOOL_READ` | `get_lead_profile_details` | bound after `tools/list` |
| `LQABR_SUMMARY_MCP_TOOL_WRITE` | `post_patch_crm` | bound after `tools/list` |
| `LQABR_SUMMARY_MCP_TOOL_LIST_LEADS` | `list_trigger_leads` | |
| `LQABR_SUMMARY_MCP_ARG_OBJECT_ID` | `object_id` | the write tool's argument name |
| `LQABR_SUMMARY_MCP_ARG_PROPERTIES` | `properties` | the write tool's argument name |
| `LQABR_SUMMARY_MCP_ASSERT_TOOLS` | `1` | `0` = start even if a tool is absent |
| `LQABR_SUMMARY_MCP_STARTUP_CHECK` | `warn` | `warn` \| `strict` \| `off` |

## HubSpot target

| Variable | Default | Notes |
|---|---|---|
| `LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE` | `ticket` | |
| `LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY` | `blog_summary` | the gateway's `R-blog-summary` trigger |
| `LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY` | `blog_industry` | drives audience fan-out |
| `LQABR_SUMMARY_DRY_RUN` | `0` | `1` = compute and log the write, send nothing |

## Fetching

| Variable | Default | Notes |
|---|---|---|
| `LQABR_SUMMARY_HTTP_TIMEOUT_SECONDS` | `15` | |
| `LQABR_SUMMARY_MAX_CHARS` | `50000` | the model's protection from a huge page |
| `LQABR_SUMMARY_MAX_RETRIES` | `3` | backoff on 429/5xx |
| `LQABR_SUMMARY_ALLOWED_HOSTS` | — | comma-separated; empty = any public host |
| `LQABR_SUMMARY_ALLOW_PRIVATE_HOSTS` | `0` | `1` only for local dev against a local service |
| `LQABR_SUMMARY_USER_AGENT` | `lqabr-summary-agent/0.1` | |

## HTTP surface

| Variable | Default | Notes |
|---|---|---|
| `LQABR_SUMMARY_ROUTES` | `all` | `all` \| `api` \| `chat` |
| `LQABR_SUMMARY_ENABLE_AGUI` | `1` | mounts `/chat` |
| `LQABR_SUMMARY_CORS_ORIGINS` | `http://localhost:5173` | comma-separated |
| `PORT` | `8080` | set by Cloud Run |

## Secrets and logging

| Variable | Default | Notes |
|---|---|---|
| `LQABR_SUMMARY_SECRETS_SOURCE` | `env` | `env` \| `secret_manager` \| `auto` |
| `LQABR_SUMMARY_GCP_PROJECT` | — | required by the Secret Manager backend |
| `LQABR_SUMMARY_LOG_LEVEL` | `INFO` | |
