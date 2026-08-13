# 00 — enable APIs + Artifact Registry repo (DEV)

Script: `00_enable_apis.sh` · Environment: **dev** (`ldqfingsrv-dev`) · Run 2026-07-21 · **done**

## What it does

Enables the 8 GCP APIs the platform needs and creates the Artifact Registry
Docker repo for agent/webhook images. Idempotent.

## Command

```bash
cd infra/gcp
source ./config.dev.sh
bash 00_enable_apis.sh
```

`config.dev.sh` echoed on source:

```
config[DEV]: project=ldqfingsrv-dev region=us-central1 sa=lqabr-agent-runtime@ldqfingsrv-dev.iam.gserviceaccount.com
```

## APIs requested

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

## Output / verification observed

```
Operation "operations/acf.p2-432617526728-98095e38-b9d8-4cac-929c-62b82ff1b7e5" finished successfully.
Create request issued for: [lqabr]
Waiting for operation [projects/ldqfingsrv-dev/locations/us-central1/operations/8d5f4832-b587-42a3-8ac7-2d7db29dcadf] to complete...done.
Created repository [lqabr].
00: APIs enabled, Artifact Registry repo 'lqabr' ready.
```

Artifact Registry repo created:

```
projects/ldqfingsrv-dev/locations/us-central1/repositories/lqabr   DOCKER
```

Optional verify:

```bash
gcloud services list --enabled --project ldqfingsrv-dev
gcloud artifacts repositories describe lqabr --location us-central1 --project ldqfingsrv-dev
```

## Deviations / gotchas

- Sourced `config.dev.sh` (dev project `ldqfingsrv-dev`), not `config.sh`.
- Billing was linked to account `01B906-D3DC6E-7DA770` (the original prod
  billing account `015999-2B94BB-C053F1` had hit its project quota), so API
  enablement proceeded without a billing block.
- Repo creation took ~a few seconds; API enable operation finished successfully.
