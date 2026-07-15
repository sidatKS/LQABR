# Config — `config.sh`

Single source of truth for the infra scripts. `source ./config.sh` before any
numbered script.

**Never record secret values here — names and metadata only.**

## How this run was driven

`config.sh` still ships the placeholder `PROJECT_ID="your-gcp-project-id"`
(the owner reserves editing that file). For this provisioning run the scripts
were driven by **environment overrides** layered on top of a normal
`source ./config.sh`, so the file on disk is unchanged:

```bash
source ./config.sh >/dev/null
export PROJECT_ID="ldqfingsrv" REGION="us-central1"
export AGENT_SA="lqabr-agent-runtime@ldqfingsrv.iam.gserviceaccount.com"
export IMAGE_BASE="us-central1-docker.pkg.dev/ldqfingsrv/lqabr"
```

Effective values: `PROJECT_ID=ldqfingsrv`, `REGION=us-central1`,
`AR_REPO=lqabr`, topics `lqabr-ingestion-trigger` / `lqabr-engagement-events`,
11 `lqabr-*` secret names (see [02-secret-manager.md](02-secret-manager.md)).

## ⚠️ Action for the team

Before developers `source ./config.sh` themselves, **`config.sh` must be set to
the real values** or they will target `your-gcp-project-id` and everything
fails. At minimum set `PROJECT_ID="ldqfingsrv"`; also fill the remaining
`CHANGE ME`s before script `05` (`MAILGUN_DOMAIN`, `TWILIO_FROM_NUMBER`,
`LQABR_SENDER_NAME`, `LQABR_CTA_URL`). Owner's call whether to commit real
values or keep them local.
