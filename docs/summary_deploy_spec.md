# LQABR Summary Agent — build, push & deploy spec

> **§0 · DOCUMENT CONTROL — the Single Source contract**
>
> | Field | Value |
> | --- | --- |
> | **Document id** | `summary_deploy_spec` |
> | **Version** | `1.0` |
> | **As of** | 2026-08-29 |
> | **Component** | `summary` → service `lqabr-dev-summary` |
> | **Companion** | `agents/summary/docs/{API,RUNBOOK,ENV_VARS,DESIGN}.md`; `docs/CloudRun_RunBook.md` P9 owns the bring-up history |
> | **Sibling** | `docs/research_deploy_spec.md` — same structure, **different secrets mode** (§4.2) |
>
> **Precedence, highest first:**
> 1. **The code** — `agents/summary/**`. If code and this document disagree, the code wins
>    and **the conflict must be reported, never silently resolved.**
> 2. **This document.**
> 3. Any prompt, summary or recollection.
>
> **Citation form:** `summary_deploy_spec §4.2`.
>
> **Provenance:** every value below was read from the files in §2 on 2026-08-29, not
> inferred. Values marked ⚠ were established by a *failure* during bring-up and are the
> ones most likely to be guessed wrong.

---

## §1 · Identity & registry — authoritative values

| Key | Value | Why this value |
| --- | --- | --- |
| `PROJECT_ID` | `ldqfingsrv-dev` | runbook D1 |
| `PROJECT_NUMBER` | `432617526728` | required for secret resource names |
| `REGION` | `us-central1` | |
| `SERVICE_NAME` | `lqabr-dev-summary` | ⚠ **was** `lqabr-summary-agent`; realigned 2026-08-26 *before* first deploy, while a rename was still free — a Cloud Run rename means a new URL |
| `AR_REPO` | `lqabr` | |
| `IMAGE` | `us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-summary` | |
| `IMAGE_TAG` | contents of `agents/summary/VERSION` → `0.1.0` | |
| `RUNTIME_SA` | `lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com` | ⚠ **was** `lqabr-agent-runtime@`, which does not exist in this project |
| `BUILD_SA` | `projects/ldqfingsrv-dev/serviceAccounts/lqabr-build@ldqfingsrv-dev.iam.gserviceaccount.com` | runbook P3 |
| `MCP_URL` | `https://lqabr-dev-mcp-432617526728.us-central1.run.app/mcp` | ⚠ **was** a guessed hostname that resolves to nothing |
| `EGRESS_IP` | `34.45.4.100` | NAT fixed IP — allowlist at Anthropic if ever needed |

---

## §2 · Context manifest — read these, do not recall

| Path | Why it is required |
| --- | --- |
| `docs/summary_deploy_spec.md` | this document |
| `agents/summary/infra/config.sh` | every value in §1 and §4 |
| `agents/summary/infra/cloudbuild.yaml` | the build contract |
| `agents/summary/infra/02_build_push.sh` | the build runner |
| `agents/summary/infra/03_deploy_run.sh` | the deploy runner — **already correct; reproduce, do not redesign** |
| `agents/summary/packages/summary_core/settings.py` | authoritative env-var names and defaults |
| `agents/summary/packages/summary_core/mcp/hubspot.py` | the write path, `_iso_published_at`, `_normalise_industry` |
| `agents/summary/src/pipeline.py` | where `blog_published_at` becomes mandatory |
| `agents/summary/docs/API.md` | request/response contract |
| `agents/summary/VERSION` | the image tag |

---

## §3 · Build & push

Build context is **`agents/summary`**, not the repo root. Two tags every build — Cloud Run
does not auto-pull `:latest`.

```bash
cd agents/summary
source infra/config.sh
bash infra/02_build_push.sh
```

which runs:

```bash
gcloud builds submit "${AGENT_DIR}" \
  --project "${PROJECT_ID}" \
  --config "${AGENT_DIR}/infra/cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE},_TAG=${IMAGE_TAG}" \
  --service-account "${BUILD_SA}"
```

⚠ **`--service-account` is mandatory.** Without it Cloud Build uses the default **compute**
SA, which holds **zero** roles here, and fails with `does not have storage.objects.get
access to ... _cloudbuild`. The Google-managed `432617526728@cloudbuild.gserviceaccount.com`
holds the right role but is rejected — *"provide a user-managed service account"*.

---

## §4 · Deploy — the complete flag set

`bash infra/03_deploy_run.sh`. Reproduce this; do not redesign it.

```bash
gcloud run deploy lqabr-dev-summary \
  --project ldqfingsrv-dev --region us-central1 \
  --image "${IMAGE}:${IMAGE_TAG}" \
  --service-account "${RUNTIME_SA}" \
  --execution-environment gen2 \
  --network lqabr-vpc --subnet lqabr-run-uscentral1 --vpc-egress all-traffic \
  --ingress internal --no-allow-unauthenticated \
  --min-instances 0 --max-instances 3 \
  --cpu 1 --memory 1Gi --timeout 300 \
  --add-volume "name=logs,type=in-memory" \
  --add-volume-mount "volume=logs,mount-path=/app/logs" \
  --set-env-vars "LQABR_SUMMARY_MCP_BASE_URL=${MCP_URL}" \
  --set-env-vars "LQABR_SUMMARY_MODEL=anthropic/claude-sonnet-5" \
  --set-env-vars "LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE=ticket" \
  --set-env-vars "LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY=blog_summary" \
  --set-env-vars "LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY=blog_industry" \
  --set-env-vars "LQABR_SUMMARY_ROUTES=all" \
  --set-env-vars "LQABR_SUMMARY_DRY_RUN=0" \
  --set-env-vars "LQABR_SUMMARY_MCP_STARTUP_CHECK=warn" \
  --set-env-vars "LQABR_SUMMARY_MCP_TOOL_READ=get_blog_summary" \
  --set-env-vars "LQABR_SUMMARY_MCP_TOOL_WRITE=upsert_blog_summary" \
  --set-env-vars "LQABR_SUMMARY_MCP_WRITE_STYLE=blog_summary" \
  --set-env-vars "LQABR_SUMMARY_SECRETS_SOURCE=secret_manager" \
  --set-env-vars "LQABR_SUMMARY_GCP_PROJECT=ldqfingsrv-dev"
```

### §4.1 · Rationale per non-obvious flag

| Flag | Reason |
| --- | --- |
| `--network` / `--subnet` / `--vpc-egress all-traffic` | ⚠ **Without these the service is not on the VPC and cannot reach the `ingress=internal` MCP at all.** `all-traffic` is verified correct — the P8a stage-3 probe proved a VPC caller reaches an internal callee; the NAT concern was disproved |
| `--ingress internal` | otherwise internet-reachable |
| `--no-allow-unauthenticated` | IAM on top of ingress |
| `--execution-environment gen2` | matches the fleet |
| `--memory 1Gi` | the log volume is tmpfs and counts against this |
| `--add-volume … /app/logs` | ⚠ `settings.py` resolves the log to `<root>/logs/agents/summary/agent.log`; `<root>` is `/app` and that tree does not exist in the image. Alternative: `LQABR_SUMMARY_LOG_FILE=""` and drop the volume — stdout already reaches Cloud Logging |
| `--timeout 300` | a run is a page fetch plus one Anthropic call. Raise it if a source is slow; the *client* timeout matters more (§9 V7) |

### §4.2 · Secrets — `secret_manager`, the OPPOSITE of research

⚠ **Do not copy `research_deploy_spec §4.2` here.**

`LQABR_SUMMARY_SECRETS_SOURCE` **must be `secret_manager`**, and
`LQABR_SUMMARY_GCP_PROJECT` **must** be set. The reason is a dependency difference, not a
preference: `agents/summary/requirements.txt:27` ships
`google-cloud-secret-manager>=2.30.0`. Research does not, which is why research must use
`env`. The model key never appears in a deploy command, a log line, or the image.

`infra/01_secrets.sh` grants `roles/secretmanager.secretAccessor` on
`lqabr-anthropic-api-key` to the runtime SA. Run it once.

### §4.3 · MCP is the only door to HubSpot for this agent

Unlike research (`research_deploy_spec §4.3`), summary holds **no** HubSpot token and makes
no direct `api.hubapi.com` call. Every read and write goes through `lqabr-dev-mcp`.

---

## §5 · MCP tool names and write style — all four must be overridden

⚠ `summary_core`'s built-in defaults (`get_lead_profile_details`, `post_patch_crm`,
write style `patch`) **do not exist on the deployed MCP** and fail the startup check.

| Setting | Code default | Required value | On the MCP? |
| --- | --- | --- | --- |
| `MCP_TOOL_READ` | `get_lead_profile_details` | `get_blog_summary` | ✅ |
| `MCP_TOOL_WRITE` | `post_patch_crm` | `upsert_blog_summary` | ✅ |
| `MCP_WRITE_STYLE` | `patch` | `blog_summary` | — |
| `MCP_STARTUP_CHECK` | `warn` | `warn` | — |

`patch` style sends `{object_id, properties{}}` and therefore demands a HubSpot object id.
`blog_summary` style sends four flat args and lets the MCP create-or-update the ticket
itself, so no caller needs to know an id.

### §5.1 · `blog_published_at` is the upsert key — and the sharpest trap here

`upsert_blog_summary` takes exactly four args, all required
(`summary_core/mcp/hubspot.py:154`):

| Arg | Source |
| --- | --- |
| `subject` | request `hubspot.subject`, else the model's title |
| `blog_summary` | `SummaryResult.as_hubspot_text()` |
| `blog_published_at` | request `hubspot.blog_published_at` → `_iso_published_at()` — **the upsert key** |
| `blog_industry` | request `hubspot.industry` or the model's, → `_normalise_industry()` |

⚠ **`_iso_published_at` passes through, unchanged, ANY value containing `T`.** It only
expands a *bare* `YYYY-MM-DD`. So:

| Input | Result | Outcome |
| --- | --- | --- |
| `2026-08-28` | `2026-08-28T00:00:00.000Z` | expanded, works |
| `2026-08-28T20:12:00.000Z` | unchanged | correct |
| `08:28:2026T20:12:00` | **unchanged** | ⚠ malformed reaches HubSpot's datetime-keyed upsert and **silently no-ops** |

Month-first or colon-separated dates are the failure. Re-running with the same key
**updates** that ticket (`action: updated`), so iterating is safe.

`blog_industry` must match the portal enum exactly — default options
`FINANCIAL_SERVICES, LEGAL_SERVICES, HEALTHCARE`, configurable via
`LQABR_SUMMARY_HUBSPOT_INDUSTRY_OPTIONS`. `_normalise_industry` coerces case and separators
only; it deliberately does **not** fuzzy-match, because a wrong-but-accepted value is worse
than a rejection.

---

## §6 · Routes

| Route | Method | Notes |
| --- | --- | --- |
| `/` | GET | identity + route index |
| `/health`, `/healthz` | GET | what this instance is bound to: `mcp.reachable`, `mcp.tools`, `hubspot.summary_property` |
| `/mcp/tools` | GET | what the MCP exposes right now — expect `missing: []` |
| `/summary/run` | POST | **the domain entry point** |
| `/summary/a2a` | POST | the gateway / orchestrator A2A envelope |
| `/chat` | POST | AG-UI streaming, only when `ROUTES=all` |

Served subset is `LQABR_SUMMARY_ROUTES` = `all` (default) \| `api` \| `chat`.

---

## §7 · Negative constraints — explicitly forbidden

| # | Forbidden | Consequence |
| --- | --- | --- |
| F1 | `LQABR_SUMMARY_SECRETS_SOURCE=env` | the model key is not resolved from Secret Manager; contradicts §4.2 |
| F2 | Leaving the `summary_core` default tool names | `mcp_startup_check_failed` — those tools do not exist |
| F3 | `--allow-unauthenticated` or `--ingress all` | makes a private agent internet-reachable |
| F4 | Omitting `--network` / `--subnet` / `--vpc-egress` | cannot reach the MCP at all |
| F5 | Omitting `--service-account` on deploy | falls back to the default compute SA |
| F6 | Omitting `--service-account` on build | build fails on the source bucket (§3) |
| F7 | Hardcoding ids, SAs, URLs or secret names in the `.sh` | duplicates config; drifts silently |
| F8 | A `blog_published_at` that is not full ISO 8601 | ⚠ silent no-op (§5.1) |
| F9 | Assuming `DRY_RUN` persists | ⚠ it defaulted back on to `1` on a redeploy once; set it explicitly every time |
| F10 | Verifying with `--freshness` instead of `timestamp>` | ⚠ stale entries twice looked like current results |
| F11 | Deleting a probe job before it finishes | ⚠ one deleted 27s in captured nothing; cold start + model call is 1-3 min |

---

## §8 · Structural & idempotency requirements

| # | Requirement |
| --- | --- |
| S1 | `bash`, beginning `set -euo pipefail` |
| S2 | `source "$(dirname "$0")/config.sh"` |
| S3 | Every value `${VAR:-default}` so the environment overrides without editing |
| S4 | No literals in the `.sh` (F7) |
| S5 | **Idempotent** — re-running redeploys the same service; `gcloud run deploy` is idempotent by service name. No create/delete logic |
| S6 | Numbered files under `agents/summary/infra/`, mode `+x` |

---

## §9 · Verification assertions

| # | Assertion |
| --- | --- |
| V1 | Resolved URL from `gcloud run services describe … --format='value(status.url)'` |
| V2 | `GET /health` → `mcp.reachable: true`, `mcp.tools`, `hubspot.summary_property` |
| V3 | `GET /mcp/tools` → `missing: []` |
| V4 | Startup events present: `service_start`, `mcp_initialized`, `mcp_tools_discovered`, **`mcp_startup_check_ok`** |
| V5 | `gcloud logging read` filtered with **`timestamp>"<deploy time>"`**, never `--freshness` (F10) |
| V6 | ⚠ A laptop `curl` returns **404 even with a valid ID token** under `ingress=internal` — that is the control working, not a fault. Escape hatch, with the instruction to revert: `gcloud run services update … --ingress all` |
| V7 | A real request needs an in-VPC caller: `bash infra/04_verify.sh` (§10, and `summary_verify_spec`) |
| V8 | The write actually landed: `get_blog_summary` on the MCP with the same `blog_published_at` returns the ticket |

---

## §10 · End-to-end test — `infra/04_verify.sh`

Fully specified in **`docs/summary_verify_spec.md`**. Three layers in one command —
control plane (gcloud), startup events (logs), and data plane (an in-VPC Cloud Run job
that calls `/health`, `/mcp/tools`, `/summary/run`, then reads the ticket back with
`get_blog_summary`).

```bash
cd agents/summary
bash infra/04_verify.sh                 # full; writes one ticket
RUN_E2E=0 bash infra/04_verify.sh       # no HubSpot write
```

Task timeout is 900s (F11). The authoritative record of what summary sent to the MCP is its
own obs log — `hubspot_write_raw_result` (carries `sent_published_at`) or
`hubspot_write_dry_run` (carries the full arg preview).

---

## §11 · Known failure modes

All four were observed on this component during P9.

| Symptom | Cause | Status |
| --- | --- | --- |
| `failed to start and listen on PORT=8080`, no detail | `settings.py:61` `parents[4]` assumed the repo layout; the Dockerfile flattens `agents/summary` to `/app`, two levels shallower → `IndexError` at **import** | ✅ fixed |
| `hubspot_write_dry_run` when a write was expected | `config.sh` defaulted `DRY_RUN=1`; a plain redeploy silently reverted it | ✅ default flipped to `0` (F9) |
| `[Errno 13] Permission denied: 'errors'` | the **MCP** image runs as user `mcp` with a root-owned `/app` and writes an `errors` path at tool-call time | ✅ fixed on the MCP by an in-memory volume at `/app/errors` |
| `blog_industry 'Healthcare' is not one of [...]` | model prose vs portal enum | ✅ `_normalise_industry()` in both write paths |
| `SecretConfigError: cannot resolve secret 'HUBSPOT_PRIVATE_APP_TOKEN'` | an **MCP-side** token-mode error, not summary's | ✅ see `infra/gcp/mcp/01_deploy.sh` |
| upsert reports success, HubSpot unchanged | malformed `blog_published_at` (§5.1) | ⚠ **not defended in code** — the guard trusts anything containing `T` |

---

## §12 · Open items

- **`_iso_published_at` does not validate.** It trusts any value containing `T`, so a
  malformed key produces a silent no-op that reports success. It should reject a value it
  cannot parse rather than forward it.
- **Dependencies are unpinned**, so two builds of one commit can differ.
- **Competing build paths** — `infra/gcp/00-07`, `infra/dev/deploy/`, and `agents/*/infra/`
  disagree on names. `agents/summary/infra/` is authoritative for this component.
