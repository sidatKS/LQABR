# LQABR Research Agent — build, push & deploy spec

> **§0 · DOCUMENT CONTROL — the Single Source contract**
>
> | Field | Value |
> | --- | --- |
> | **Document id** | `research_deploy_spec` |
> | **Version** | `1.1` |
> | **As of** | 2026-08-27 |
> | **Component** | `research` → service `lqabr-dev-research` |
> | **Companion** | `docs/research_image_spec.md` (image internals; this doc owns build/push/deploy) |
> | **Supersedes** | `docs/SETUP_CLOUDRUN_IAM.md` for this component — that doc targets a different project and a six-SA model |
>
> **Precedence, highest first:**
> 1. **The code** — `agents/research/**`. If code and this document disagree, the code wins
>    and **the conflict must be reported, never silently resolved.**
> 2. **This document.**
> 3. Any prompt, summary or recollection.
>
> **Citation form:** `research_deploy_spec §4.2`. Every section below is stable and
> individually addressable; do not cite by page or heading text.
>
> **Provenance:** every value here was read from the source files listed in §2 on
> 2026-08-26, not inferred. Values marked ⚠ were established by a *failure* during the
> `summary`/`mcp` bring-up and are the ones most likely to be guessed wrong.

---

## §1 · Identity & registry — authoritative values

| Key | Value | Why this value |
| --- | --- | --- |
| `PROJECT_ID` | `ldqfingsrv-dev` | runbook D1 |
| `PROJECT_NUMBER` | `432617526728` | required for secret resource names |
| `REGION` | `us-central1` | |
| `SERVICE_NAME` | `lqabr-dev-research` | live convention `lqabr-dev-<component>` |
| `AR_REPO` | `lqabr` | exists since 2026-07-21 |
| `IMAGE` | `us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-research` | |
| `IMAGE_TAG` | contents of `agents/research/VERSION` → `0.1.0` | |
| `RUNTIME_SA` | `lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com` | runbook D2 — one runtime SA for all services |
| `BUILD_SA` | `projects/ldqfingsrv-dev/serviceAccounts/lqabr-build@ldqfingsrv-dev.iam.gserviceaccount.com` | runbook P3 |
| `MCP_URL` | `https://lqabr-dev-mcp-432617526728.us-central1.run.app/mcp` | deployed 2026-08-26 |
| `EGRESS_IP` | `34.45.4.100` | NAT fixed IP — allowlist at vendors |

---

## §2 · Context manifest — read these, do not recall

**Every file below must be read in the current turn.** Nothing in this section may be
answered from memory of a previous turn or from this document alone.

| Path | Why it is required |
| --- | --- |
| `docs/research_deploy_spec.md` | this document |
| `agents/research/infra/config.sh` | the file being extended |
| `agents/research/infra/cloudbuild.yaml` | build contract, already written |
| `agents/research/infra/02_build_push.sh` | the build runner to mirror |
| `agents/summary/infra/03_deploy_run.sh` | **the house pattern to mirror** |
| `agents/summary/infra/config.sh` | house pattern for config split |
| `agents/research/packages/research_core/settings.py` | authoritative env-var names and defaults |
| `agents/research/packages/research_core/secrets.py` | the `_env_name` mapping in §4.2 |
| `agents/research/config/config.yaml` | ships in the image; sets defaults for unset vars |
| `agents/research/VERSION` | the image tag |

---

## §3 · Build & push

Context is **`agents/research`**, not the repo root (see `research_image_spec §5.1`).
Two tags every build — Cloud Run does not auto-pull `:latest`.

```bash
gcloud builds submit agents/research \
  --project ldqfingsrv-dev \
  --config agents/research/infra/cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE},_TAG=${IMAGE_TAG}" \
  --service-account "${BUILD_SA}"
```

⚠ **`--service-account` is mandatory.** Without it Cloud Build uses the default **compute**
SA, which holds **zero** roles in this project, and fails with
`does not have storage.objects.get access to ... _cloudbuild`. The Google-managed
`432617526728@cloudbuild.gserviceaccount.com` holds the right role but is rejected —
*"provide a user-managed service account"*.

---

## §4 · Deploy — the complete flag set

Reproduce this. Do not redesign it.

```bash
gcloud run deploy lqabr-dev-research \
  --project ldqfingsrv-dev \
  --region us-central1 \
  --image "${IMAGE}:${IMAGE_TAG}" \
  --service-account "${RUNTIME_SA}" \
  --execution-environment gen2 \
  --network lqabr-vpc \
  --subnet lqabr-run-uscentral1 \
  --vpc-egress all-traffic \
  --ingress internal \
  --no-allow-unauthenticated \
  --port 8080 \
  --cpu 1 --memory 1Gi \
  --timeout 600 \
  --min-instances 0 --max-instances 3 \
  --add-volume "name=logs,type=in-memory" \
  --add-volume-mount "volume=logs,mount-path=/app/logs" \
  --set-secrets "LQABR_ANTHROPIC_API_KEY=lqabr-anthropic-api-key:latest,LQABR_HUBSPOT_ACCESS_TOKEN=lqabr-hubspot-access-token:latest" \
  --set-env-vars "LQABR_RESEARCH_SECRETS_SOURCE=env" \
  --set-env-vars "LQABR_RESEARCH_MCP_BASE_URL=${MCP_URL}" \
  --set-env-vars "LQABR_RESEARCH_LOG_FORMAT=json" \
  --set-env-vars "LQABR_RESEARCH_DRY_RUN=0"
```

### §4.1 · Rationale per non-obvious flag

| Flag | Reason |
| --- | --- |
| `--network` / `--subnet` / `--vpc-egress all-traffic` | ⚠ **Without these the service is not on the VPC and cannot reach the `ingress=internal` MCP at all.** `all-traffic` is verified correct — the P8a probe proved a VPC caller reaches an internal callee; the concern that NAT would break it was disproved |
| `--ingress internal` | otherwise internet-reachable |
| `--no-allow-unauthenticated` | IAM enforced on top of ingress |
| `--execution-environment gen2` | matches the fleet |
| `--timeout 600` | ⚠ a run is a page fetch **plus** an Anthropic call with web search (`search_max_uses=5`, `search_timeout_seconds=90`). The 300s default is too tight |
| `--memory 1Gi` | the log volume is tmpfs and counts against this |
| `--add-volume … /app/logs` | ⚠ `config.yaml` sets `logging.file: logs/agents/research/agent.log` → `/app/logs/...`, a tree that does not exist in the image. **Alternative: set `LQABR_RESEARCH_LOG_FILE=""` and drop the volume** — stdout already reaches Cloud Logging and nothing reads the file |

### §4.2 · Secrets — `env` source, never `secret_manager`

⚠ **The single most likely mistake in this deploy.**

`LQABR_RESEARCH_SECRETS_SOURCE` **must be `env`.** `secret_manager` looks more correct and
**fails at runtime**: `agents/research/requirements.txt` does **not** include
`google-cloud-secret-manager`, so `research_core/secrets.py` raises on
`from google.cloud import secretmanager`. Research is standalone and never receives
`lqabr_core[gcp]`.

With `source=env`, `resolve_secret()` reads an env var named by uppercasing the secret name
and converting hyphens to underscores (`secrets.py:_env_name`):

| Secret in Secret Manager | Env var it is read from |
| --- | --- |
| `lqabr-anthropic-api-key` | `LQABR_ANTHROPIC_API_KEY` |
| `lqabr-hubspot-access-token` | `LQABR_HUBSPOT_ACCESS_TOKEN` |

`--set-secrets` injects the values under exactly those names, so the runtime SA still gates
access and no token enters the image. **Do not set `LQABR_RESEARCH_GCP_PROJECT`** — it
applies only to the `secret_manager` source.

### §4.3 · Why research holds a HubSpot token (not an error)

Research reads HubSpot **directly** via `research_core/hubspot_direct.py` →
`https://api.hubapi.com`, because `use_direct_lead_lookup` defaults to `True`. This is the
one documented exemption from "MCP is the only door to HubSpot", and
`tests/test_standalone.py` enforces that it stays **read-only**. Reason, from
`mcp/hubspot.py:105-109`: the MCP exposes no lead-listing tool.

Set `LQABR_RESEARCH_USE_DIRECT_LEAD_LOOKUP=false` **only** once the MCP grows
`list_leads_by_industry`; otherwise the campaign cannot find leads.

---

## §5 · MCP tool names — verified against the deployed MCP

The MCP exposes exactly four tools. Research's defaults already match — **no overrides
needed**, unlike `summary`, whose defaults were stale and failed its startup check.

| Setting | Default | On the MCP? |
| --- | --- | --- |
| `mcp_tool_read_lead` | `get_lead_profile` | ✅ |
| `mcp_tool_read_blog` | `get_blog_summary` | ✅ |
| `mcp_tool_write` | `upsert_lead_profile` | ✅ |
| `mcp_tool_list_leads` | `list_leads_by_industry` | ❌ **deliberately not asserted** |
| `mcp_object_id_arg` | `objectId` | ✅ matches the MCP's "reader accepts: objectId" |

`ensure_ready()` asserts only the first three (`mcp/hubspot.py:110`) — *"Asserting it would
refuse to start over a tool nothing calls."*

---

## §6 · Environment surface (39 vars) and routes

Only the four in §4 need setting. **Precedence: environment > `config/config.yaml` > code
default.** The config map ships in the image, so an unset var is not undefined.

All names are prefixed `LQABR_RESEARCH_`. **\*** = set explicitly in §4.

**Model** — `MODEL` (`claude-sonnet-4-6`), `MAX_TOKENS` (2000), `MODEL_TOKEN_SECRET`
(`lqabr-anthropic-api-key`)

**MCP** — `MCP_BASE_URL`**\***, `MCP_TIMEOUT_SECONDS` (60), `MCP_AUTH_TOKEN` (empty — leave
it; the Google ID token is attached automatically by `auth_header()`),
`MCP_PROTOCOL_VERSION` (`2025-06-18`), `MCP_TOOL_READ_LEAD`, `MCP_TOOL_READ_BLOG`,
`MCP_TOOL_WRITE`, `MCP_TOOL_LIST_LEADS`, `MCP_OBJECT_ID_ARG` (`objectId`),
`MCP_ASSERT_TOOLS` (true), `MCP_STARTUP_CHECK` (`warn`), `MAX_RETRIES` (3),
`MCP_BACKOFF_BASE_SECONDS` (1.0), `MCP_BACKOFF_CAP_SECONDS` (8.0),
`MCP_RETRYABLE_STATUSES` (429,500,502,503,504)

**HubSpot** — `HUBSPOT_CONTEXT_PROPERTY` (`lead_context`), `DRY_RUN`**\***,
`SKIP_IF_CONTEXT_PRESENT` (false), `HUBSPOT_BASE_URL` (empty → `https://api.hubapi.com`),
`HUBSPOT_TOKEN_SECRET` (`lqabr-hubspot-access-token`), `HUBSPOT_TIMEOUT_SECONDS` (30),
`USE_DIRECT_LEAD_LOOKUP` (true)

**Search** — `SEARCH_ENABLED` (true), `SEARCH_MAX_USES` (5), `SEARCH_TIMEOUT_SECONDS` (90),
`SEARCH_ALLOWED_DOMAINS` (—), `SEARCH_BLOCKED_DOMAINS` (—)

**Output** — `NOTE_MAX_CHARS` (60000), `NOTE_TARGET_WORDS` (160), `CORS_ORIGINS`

**Secrets** — `SECRETS_SOURCE`**\***, `GCP_PROJECT` (leave empty — see §4.2)

**Logging** — `LOG_LEVEL` (INFO), `LOG_FILE`, `LOG_FORMAT`**\***, `LOG_DETAIL` (true)

**Routing** — `ROUTE_CAMPAIGN_A2A` (`/research/campaign/a2a`)

### §6.1 · Routes

| Route | Method | Notes |
| --- | --- | --- |
| `/health` | GET | Cloud Run health check |
| `/mcp/tools` | GET | discovered tool list — useful post-deploy check |
| `/research/campaign/a2a` | POST | **the gateway's entry point**, from `route_campaign_a2a` |

---

## §7 · Negative constraints — explicitly forbidden

Anything in this list is a defect, not a preference.

| # | Forbidden | Consequence if done |
| --- | --- | --- |
| F1 | `LQABR_RESEARCH_SECRETS_SOURCE=secret_manager` | runtime `ImportError` — dependency absent (§4.2) |
| F2 | Setting `LQABR_RESEARCH_GCP_PROJECT` | no effect under `source=env`; implies the wrong mode |
| F3 | `--allow-unauthenticated` or `--ingress all` | makes a private agent internet-reachable |
| F4 | Omitting `--network` / `--subnet` / `--vpc-egress` | service cannot reach the MCP at all |
| F5 | Omitting `--service-account` on **deploy** | falls back to the default compute SA |
| F6 | Omitting `--service-account` on **build** | build fails on the source bucket (§3) |
| F7 | Hardcoding project ids, SAs, URLs or secret names in the `.sh` | duplicates config; drifts silently |
| F8 | Creating any IAM binding, secret, bucket or other GCP resource | out of scope; needs its own review |
| F9 | Any flag not listed in §4 | unreviewed surface |
| F10 | `--set-env-vars` used in a way that **replaces** rather than adds | silently drops earlier vars; prefer repeated flags or `--update-env-vars` |
| F11 | Verifying with `--freshness` instead of `timestamp>` | ⚠ stale entries twice looked like current results during bring-up |
| F12 | Executing the script as part of generating it | mutates cloud state before review |

---

## §8 · Structural & idempotency requirements

| # | Requirement |
| --- | --- |
| S1 | `bash`, beginning `set -euo pipefail` |
| S2 | `source "$(dirname "$0")/config.sh"` — mirror `agents/summary/infra/03_deploy_run.sh` |
| S3 | Every value `${VAR:-default}` so the environment overrides without editing |
| S4 | No literals in the `.sh`; all values live in `config.sh` (F7) |
| S5 | One `# why:` comment per flag listed in §4.1 |
| S6 | **Idempotent** — re-running redeploys the same service; it never creates a second one. `gcloud run deploy` is idempotent by service name; do not add create/delete logic |
| S7 | File path exactly `agents/research/infra/03_deploy_run.sh`, mode `+x` |
| S8 | Exit criterion: emit the file contents, then **stop**. No execution, no summary prose, no follow-up offers |

---

## §9 · Verification assertions the script must emit

After a successful deploy the script prints — it does not run — the following.

| # | Assertion |
| --- | --- |
| V1 | The resolved service URL, from `gcloud run services describe … --format='value(status.url)'` |
| V2 | `gcloud run services logs read lqabr-dev-research --project … --region … --limit 30` |
| V3 | A `gcloud logging read` filtered with **`timestamp>"<deploy time>"`** — never `--freshness` (F11) |
| V4 | The four expected events: `service_start`, `mcp_initialized`, `mcp_tools_discovered`, **`mcp_startup_check_ok`** |
| V5 | An explicit warning that a laptop `curl` returns **404 even with a valid ID token** under `ingress=internal` — *that is the control working, not a fault* |
| V6 | The temporary-access escape hatch, with the instruction to revert: `gcloud run services update … --ingress all` |
| V7 | The in-VPC invocation note: reaching the service for a real request needs a Cloud Run job on `lqabr-vpc` (pattern in `CloudRun_RunBook §0.2`) |

---

## §10 · Known failure modes — pre-empted or not

Observed on `summary` / `mcp` during bring-up.

| Symptom | Cause | Status for research |
| --- | --- | --- |
| `failed to start and listen on PORT=8080`, no detail | `settings.py` `parents[4]` assumed the repo layout → `IndexError` at import | ✅ fixed in `research_core/settings.py` |
| `mcp_startup_check_failed` | stale tool names | ✅ defaults match the live MCP (§5) |
| `SecretConfigError: cannot resolve secret` | wrong secret-resolution mode | ✅ §4.2 |
| `[Errno 13] Permission denied` | non-writable path | ✅ log volume (§4) |
| `hubspot_write_dry_run` when a write was expected | `DRY_RUN` defaulted back on redeploy | ✅ set explicitly to `0` |
| `blog_industry '<Value>' is not one of [...]` | enum casing | ⚠ research writes `lead_context`, not `blog_industry` — **not applicable**, but any enum property it writes needs the portal's exact spelling |

---

## §11 · Open items

- ~~**`03_deploy_run.sh` does not exist yet**~~ **CLOSED 2026-08-29** — it exists and the
  live service matches this spec exactly (verified: `ingress=internal`, on `lqabr-vpc`,
  SA `lqabr-agent-dev`, `SECRETS_SOURCE=env`, both secrets bound, `/app/logs` mounted,
  `DRY_RUN=0`). `01_secrets.sh` and `04_verify.sh` were added alongside it.
- **Verification is now specified** — `docs/research_verify_spec.md`, executable as
  `bash infra/04_verify.sh`. ⚠ Its default mode runs **no campaign**: a campaign writes
  `lead_context` to every lead in an industry, and each write trips the gateway into the
  Email agent.
- **Dependencies are unpinned** (`anthropic>=0.40`, `fastapi>=0.115`, …), so two builds of
  one commit can differ. Lockfiles proposed and deferred.
- **Competing build paths** — `infra/gcp/00-07`, `infra/dev/deploy/`,
  `infra/docker-compose.yml` and `agents/*/infra/` disagree on names. Pick one, deprecate
  the rest.
- ~~**MCP has no deploy script.**~~ **CLOSED 2026-08-29** — `infra/gcp/mcp/`
  (`config.sh`, `00_promote_image.sh`, `01_deploy.sh`, `02_probe.sh`) now owns the MCP's
  promote/deploy/verify path, including `LQABR_SECRET_PROJECT` /
  `LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN` and the `/app/errors` volume.
- **Summary now has a peer spec** — `docs/summary_deploy_spec.md`. Note its §4.2 is the
  OPPOSITE of §4.2 here: summary ships `google-cloud-secret-manager` and must use
  `secret_manager`; research does not and must use `env`.
