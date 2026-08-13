# 01 — runtime service account + least-privilege roles (DEV)

Script: `01_service_accounts.sh` · Environment: **dev** (`ldqfingsrv-dev`) · Run 2026-07-21 · **done**

## What it does

Creates the agent runtime service account and grants it least-privilege
runtime roles at project scope. Idempotent (`describe || create`, bindings are
add-only). This SA is the identity every Cloud Run service runs as.

Dev SA is named **`lqabr-agent-dev`** (prod uses `lqabr-agent-runtime`) so it's
identifiable by `principalEmail` in logs / process signatures without checking
the project.

## Command

```bash
cd infra/gcp
source ./config.dev.sh
bash 01_service_accounts.sh
```

## Service account

```
lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com
```

## Runtime roles granted (project scope)

```
roles/secretmanager.secretAccessor   # read service credentials from Secret Manager
roles/pubsub.publisher               # ingestion trigger + engagement event fan-out
roles/pubsub.subscriber
roles/run.invoker                    # orchestrator invokes stage agents (A2A)
roles/aiplatform.user                # Gemini via Vertex AI
roles/logging.logWriter
roles/monitoring.metricWriter
```

## Output / verification observed

```
Updated IAM policy for project [ldqfingsrv-dev].   (x7)
01: service account lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com ready with runtime roles.
```

Optional verify:

```bash
gcloud iam service-accounts describe lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com --project ldqfingsrv-dev
gcloud projects get-iam-policy ldqfingsrv-dev \
  --flatten="bindings[].members" \
  --filter="bindings.members:lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com" \
  --format="table(bindings.role)"
```

## Deviations / gotchas

- **First run failed on SA propagation lag:** the SA was created, but the
  immediately-following `add-iam-policy-binding` returned
  `INVALID_ARGUMENT: Service account ... does not exist` because the new SA had
  not propagated yet. Fix: waited ~20s and re-ran the (idempotent) script — it
  skipped SA creation and applied all 7 bindings successfully.
- Script hardcodes display name `"LQABR agent runtime"`, so the dev SA's email
  is `lqabr-agent-dev` (what matters for attribution) while its display name
  stays generic.
- Human developer access (dev group roles incl. `secretmanager.viewer` +
  `secretAccessor`) is a **separate** grant, not part of this script.
