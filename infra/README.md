# infra/ — LQABR infrastructure & deployment

Environment-first layout. **Dev and prod are fully self-contained — no shared
files, no overlap.** Everything needed for an environment lives under that
environment's folder; scripts are intentionally repeated per env.

```
infra/
├─ dev/
│  ├─ gcp/         # infra SETUP via gcloud scripts (00–04 + config.sh + setup/)
│  ├─ terraform/   # infra SETUP via IaC (same resources as gcp/, apply per env)
│  └─ deploy/      # DEPLOYMENT — deploy CI-built images to Cloud Run + wiring
└─ prod/
   ├─ gcp/
   ├─ terraform/
   └─ deploy/
```

## The three layers

1. **CI (separate, not in this folder)** builds the service images and pushes
   them to Artifact Registry. This planner does not own CI.
2. **Setup** (`gcp/` **or** `terraform/`) stands up the project: APIs, runtime
   service account + IAM, developer-group access, Artifact Registry repo,
   Secret Manager containers, Pub/Sub. Run once per environment (owner).
   Pick gcloud scripts **or** Terraform — they provision the same resources.
3. **Deploy** (`deploy/`) deploys the pre-built images to Cloud Run in the SP-2
   topology (public Agent Gateway + shared HubSpot MCP + internal OIDC agents),
   wires service URLs, and binds public access on the Gateway. Run per release.

## Typical flow (per environment)

```bash
cd infra/dev/gcp        # or infra/dev/terraform
source ./config.sh && bash 00_enable_apis.sh 01_service_accounts.sh \
                            02_secret_manager.sh 03_pubsub.sh
python 04_hubspot_properties.py
# … CI pushes images to Artifact Registry …
cd ../deploy
source ./config.sh && bash deploy.sh && bash verify.sh
```

Architecture: **SP-2** — one public door (Agent Gateway, signature-verified),
everything else internal + OIDC, one shared HubSpot MCP write path, scale-to-zero.
Voice is **Vapi**. See each folder's `README.md` for details.
