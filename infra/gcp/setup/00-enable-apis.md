# 00 — enable APIs + Artifact Registry repo

Script: `00_enable_apis.sh` · Run 2026-07-15 · **done**

## What it does

Enables the 8 GCP APIs the platform needs and creates the Artifact Registry
Docker repo for agent/webhook images. Idempotent.

## Command

```bash
source ./config.sh >/dev/null
export PROJECT_ID="ldqfingsrv" REGION="us-central1" \
  AGENT_SA="lqabr-agent-runtime@ldqfingsrv.iam.gserviceaccount.com" \
  IMAGE_BASE="us-central1-docker.pkg.dev/ldqfingsrv/lqabr"
bash 00_enable_apis.sh
```

## Output / verification observed

APIs enabled (verified via `gcloud services list --enabled`):

```
aiplatform.googleapis.com
artifactregistry.googleapis.com
cloudbuild.googleapis.com
cloudscheduler.googleapis.com
iam.googleapis.com
pubsub.googleapis.com
run.googleapis.com
secretmanager.googleapis.com
```

Artifact Registry repo created:

```
projects/ldqfingsrv/locations/us-central1/repositories/lqabr   DOCKER   (0.000MB)
```

## Deviations / gotchas

- Ran via env-override (see [config.md](config.md)); `config.sh` unchanged.
- Repo creation took ~60s; API enable was quick (billing already linked, so no
  enablement block).
