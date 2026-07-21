# LQABR Dev Environment — Full Setup Reference

Complete record of the `ldqfingsrv-dev` dev environment as provisioned on
2026-07-21. Branch: `leadq-dev`. Config: `infra/gcp/config.dev.sh`
(sourced instead of `config.sh`). No secret values are recorded here.

---

## 1. Project setup

| Item | Value |
|---|---|
| Project ID | `ldqfingsrv-dev` |
| Project name | LQABR Dev |
| Project number | `432617526728` |
| Region | `us-central1` |
| Billing account | `01B906-D3DC6E-7DA770` ("My Billing Account") |
| Labels | `environment=dev, app=lqabr, owner=platform, cost-center=engineering` |
| Config file | `infra/gcp/config.dev.sh` (`LQABR_ENV=dev`) |

Note: prod's billing account `015999-2B94BB-C053F1` had hit its project quota,
so dev was linked to `01B906-...` instead.

---

## 2. Google APIs enabled (9)

From step `00_enable_apis.sh` (8):

```
run.googleapis.com
cloudbuild.googleapis.com
artifactregistry.googleapis.com
secretmanager.googleapis.com
pubsub.googleapis.com
cloudscheduler.googleapis.com
aiplatform.googleapis.com
iam.googleapis.com
```

Enabled later while querying group membership (9th):

```
cloudidentity.googleapis.com
```

**Artifact Registry:** Docker repo `lqabr` in `us-central1`
(`us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr`).

---

## 3. Runtime service account (machine identity)

| Item | Value |
|---|---|
| SA email | `lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com` |
| Purpose | identity the deployed Cloud Run agents run as |
| Named `-dev` | so it's identifiable by `principalEmail` in logs vs prod `lqabr-agent-runtime` |

**Runtime roles (7), project scope:**

```
roles/secretmanager.secretAccessor   # read secret values
roles/pubsub.publisher               # ingestion trigger + engagement fan-out
roles/pubsub.subscriber
roles/run.invoker                    # orchestrator → stage agents (A2A)
roles/aiplatform.user                # Vertex AI (if used)
roles/logging.logWriter
roles/monitoring.metricWriter
```

---

## 4. Developer access — group `ai2d@aidefinitive.com`

**Members (verified 2026-07-21):**

| Member | Group role |
|---|---|
| `aidcld@aidefinitive.com` | MEMBER (shared developer login) |
| `swaroop@aidefinitive.com` | OWNER + MEMBER |

**Project-scope roles (8) granted to the group:**

```
roles/run.developer                      # redeploy Cloud Run revisions
roles/cloudbuild.builds.editor           # build images
roles/artifactregistry.writer            # push/pull images
roles/secretmanager.viewer               # SEE/list secret names (dev-only add)
roles/secretmanager.secretAccessor       # READ secret values
roles/logging.viewer
roles/monitoring.viewer
roles/serviceusage.serviceUsageConsumer
```

**Resource-scoped binding:**

```
roles/iam.serviceAccountUser  on  lqabr-agent-dev  (deploy Cloud Run as the SA)
```

**Withheld (least-privilege):** no `owner`/`editor`, no `*.admin`, no project
IAM-admin. **Deferred:** `storage.objectAdmin` on `gs://ldqfingsrv-dev_cloudbuild`
(bucket created on first build, step 05).

Difference from prod: dev adds `secretmanager.viewer` so developers can
`gcloud secrets list --project ldqfingsrv-dev` and see keys before using them.

---

## 5. Secret Manager (12 containers)

| Secret | Value? | Used by |
|---|---|---|
| `lqabr-anthropic-api-key` | ✅ v1 | all agents' LLM calls (Claude, Path A) |
| `lqabr-hubspot-access-token` | ✅ v1 | Lead Profile + all agents (system of record) |
| `lqabr-mailgun-api-key` | ✅ v1 | Email agent (send) |
| `lqabr-mailgun-webhook-signing-key` | ✅ v1 | Email webhook signature verify |
| `lqabr-twilio-account-sid` | ✅ v1 | Text/Voice agent |
| `lqabr-twilio-auth-token` | ✅ v1 | Text/Voice agent + Twilio signature |
| `lqabr-zoominfo-username` | ⏸ empty | Ingestion (ZoomInfo source) — SSO blocked |
| `lqabr-zoominfo-password` | ⏸ empty | Ingestion (ZoomInfo source) — SSO blocked |
| `lqabr-zoom-account-id` | ⏸ empty | Scheduling agent — no dev developer access |
| `lqabr-zoom-client-id` | ⏸ empty | Scheduling agent |
| `lqabr-zoom-client-secret` | ⏸ empty | Scheduling agent |
| `lqabr-zoom-webhook-secret-token` | ⏸ empty | Scheduling webhook |

Model provider: swapped `lqabr-google-api-key` → `lqabr-anthropic-api-key`
(agents use Claude, not Gemini).

---

## 6. MVP data flow

Target MVP: **CSV load → build lead profiles in HubSpot → agents retrieve for
processing.**

```
CSV seeds (data/seeds/b2b/)
      │  Ingestion Agent  (--source csv)
      ▼
Lead Profile Agent → 9-pointer profile → HubSpot (system of record)
      │
      ▼
Outreach agents read leads from HubSpot and act:
   Email (Mailgun) → probability↑
   Text/Voice (Twilio, leads ≥30)
   Scheduling (Zoom, leads ≥60)
Orchestrator routes stages via A2A.
```

---

## 7. Agent readiness (given current dev secrets)

| Agent | Secrets ready? | MVP status |
|---|---|---|
| **Ingestion (CSV)** | none needed | ✅ Ready — CSV source works now |
| Ingestion (ZoomInfo) | ❌ empty | Blocked — SSO; use CSV for MVP |
| **Lead Profile → HubSpot** | ✅ HubSpot + Anthropic | ✅ Ready once model code fix lands |
| **Email (Mailgun)** | ✅ Mailgun | ✅ Ready (after deploy + model fix) |
| **Text/Voice (Twilio)** | ✅ Twilio creds, ⚠ no `TWILIO_FROM_NUMBER` | Partial — set dev sending number |
| **Scheduling (Zoom)** | ❌ empty | Blocked — need Zoom S2S OAuth app |
| **Orchestrator (A2A)** | needs agent URLs | Ready after agents deployed |

**MVP path (CSV → HubSpot → retrieve) is workable** with today's secrets:
ingestion (CSV) + lead profile + HubSpot are all provisioned.

---

## 8. Open prerequisites before agents run end-to-end

1. **Anthropic model code change (blocking all LLM calls):** agents currently
   pass a bare model string (`model=MODEL`), which only works for Gemini. Add a
   shared `build_model()` helper wrapping non-Gemini models in ADK `LiteLlm`,
   plus `litellm` in each `requirements.txt`, and set `LQABR_<AGENT>_MODEL` to a
   Claude string. Until done, agents can't call the model.
2. **Deploy (step 05):** not yet run — no Cloud Run services exist; agents run
   locally (`adk run`) only. Deploy also creates the Cloud Build bucket
   (then grant the deferred `storage.objectAdmin`).
3. **Dev runtime config:** `TWILIO_FROM_NUMBER`, `MAILGUN_DOMAIN`, sender/CTA in
   `config.dev.sh` still hold placeholders — set real dev values before outreach.
4. **HubSpot properties (step 04):** `lqabr_*` contact properties must be
   bootstrapped in the dev HubSpot before profiles upsert cleanly.
5. **Parked integrations:** ZoomInfo (SSO) and Zoom (developer access) — admin
   action needed.

---

## 9. Provisioning steps completed

| Step | Script | Status |
|---|---|---|
| Config | `config.dev.sh` | done |
| 00 | `00_enable_apis.sh` | done (APIs + Artifact Registry) |
| 01 | `01_service_accounts.sh` | done (`lqabr-agent-dev` + 7 roles) |
| Access | dev-group IAM grant | done (`ai2d@` + 8 roles + actAs) |
| 02 | `02_secret_manager.sh` | done (6/12 populated) |
| 03 | `03_pubsub.sh` | pending |
| 04 | `04_hubspot_properties.py` | pending |
| 05 | `05_deploy_agents.sh` | pending (needs model code fix) |
| 06 | `06_cloud_scheduler.sh` | pending |
