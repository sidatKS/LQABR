# gcp/ — infra SETUP (gcloud scripts)

Numbered, idempotent gcloud scripts that stand up the LQABR **infrastructure**
for THIS environment (project defined in `config.sh`). This is the *setup*
layer only — it does **not** build images (CI does that) and does **not** deploy
services (see `../deploy/`). The Terraform equivalent lives in `../terraform/`;
use gcloud **or** Terraform, not both, for a given resource.

Self-contained: every script sources the local `./config.sh`. No cross-
environment references.

## Run order (owner runs 00–01; developers can re-run 02–04)

```bash
source ./config.sh
bash 00_enable_apis.sh        # APIs (incl. iamcredentials, cloudidentity) + Artifact Registry repo
bash 01_service_accounts.sh   # runtime SA + roles, developer-group grants + actAs
bash 02_secret_manager.sh     # active secrets (prompted) + parked containers
bash 03_pubsub.sh             # ingestion + engagement topics (planned spine)
pip install -e <path-to>/lqabr_core   # once, for the next step
python 04_hubspot_properties.py        # bootstrap lqabr_* contact properties
```

## What this creates

- **APIs** enabled on the project.
- **Artifact Registry** docker repo (`lqabr`) — CI pushes images here.
- **Runtime service account** with least-privilege runtime roles.
- **Developer group** deploy-and-operate roles + `actAs` on the runtime SA.
- **Secret Manager** secret containers (values added interactively / out-of-band).
- **Pub/Sub** topics + a pull subscription (planned event spine).
- **HubSpot** `lqabr_*` contact properties (incl. `lqabr_lead_context`).

Once setup is done and CI has pushed images to Artifact Registry, deploy with
`../deploy/deploy.sh`.
