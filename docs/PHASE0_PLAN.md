# Phase 0 — Foundation & Governance (E0)

**Objective:** a GCP project, secrets, identity, and HubSpot schema ready
for the agents to run — nothing lead-facing yet.

## Steps

1. Complete `docs/PREREQUISITES.md` §1–§3 (accounts, tooling, GCP auth).
2. Edit `infra/gcp/config.sh` — PROJECT_ID, REGION, Mailgun domain/from,
   Twilio number, sender name.
3. From `infra/gcp/`:

   ```bash
   source ./config.sh
   bash 00_enable_apis.sh          # APIs + Artifact Registry
   bash 01_service_accounts.sh     # lqabr-agent-runtime SA + roles
   bash 02_secret_manager.sh       # 11 service secrets (prompts for values)
   bash 03_pubsub.sh               # ingestion + engagement topics
   pip install -e ../../packages/lqabr_core
   python 04_hubspot_properties.py # lqabr_* contact properties in HubSpot
   ```

## Verification (Definition of Done)

```bash
# All 11 secrets exist and have a version:
gcloud secrets list --project $PROJECT_ID | grep -c lqabr-   # -> 11

# Runtime SA exists with secretAccessor:
gcloud projects get-iam-policy $PROJECT_ID \
  --flatten bindings --filter "bindings.members:lqabr-agent-runtime" \
  --format 'value(bindings.role)' | grep secretAccessor

# Pub/Sub topics:
gcloud pubsub topics list --project $PROJECT_ID | grep lqabr

# HubSpot: Settings -> Properties -> filter group "LQABR" -> 19 properties.
```

Local: `python3 -m pytest -c tests/pytest.ini -q` passes (64 tests, no
credentials needed).

**Exit:** Phase 1 (ingestion → profile → HubSpot) can start.
