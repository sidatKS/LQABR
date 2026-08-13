# infra/gcp — LQABR platform provisioning

Numbered, idempotent scripts that stand up the LQABR runtime on Google
Cloud: Secret Manager for every service credential (MailGun, Twilio,
HubSpot, ZoomInfo, Zoom), the agent runtime service account, Pub/Sub
topics, Cloud Run (managed serverless sandbox) hosting for the ADK agents
and webhook receivers, and Cloud Scheduler triggers.

## Run order

Edit `config.sh` first (PROJECT_ID, region, Mailgun domain, Twilio number,
sender identity), then from this directory:

```bash
source ./config.sh
bash 00_enable_apis.sh        # APIs + Artifact Registry repo
bash 01_service_accounts.sh   # lqabr-agent-runtime SA + least-privilege roles
bash 02_secret_manager.sh     # create the 11 service secrets (prompts for values)
bash 03_pubsub.sh             # ingestion-trigger + engagement-events topics
pip install -e ../../packages/lqabr_core   # once, for the next step
python 04_hubspot_properties.py            # bootstrap lqabr_* contact properties
bash 05_deploy_agents.sh      # build + deploy all agents & webhooks to Cloud Run
bash 06_cloud_scheduler.sh    # dispatch cycle (30 min) + daily ZoomInfo pull
```

Every script is safe to re-run. Secrets are created empty-skippable and
rotated by adding new versions — never by editing code or configs.

## What gets deployed

| Cloud Run service | Kind | Auth |
|---|---|---|
| `lqabr-ingestion-agent` | ADK api_server | internal (SA invoker) |
| `lqabr-lead-profile-agent` | ADK api_server | internal |
| `lqabr-email-agent` | ADK api_server | internal |
| `lqabr-text-voice-agent` | ADK api_server | internal |
| `lqabr-scheduling-agent` | ADK api_server | internal |
| `lqabr-orchestrator-agent` | ADK api_server | internal (Scheduler OIDC) |
| `lqabr-email-webhook` | FastAPI (Mailgun events) | public + signature check |
| `lqabr-text-voice-webhook` | FastAPI (Twilio TwiML/status) | public + signature check |
| `lqabr-scheduling-webhook` | FastAPI (Zoom events) | public + signature check |

After `05`, the script prints the webhook URLs to paste into the Mailgun and
Zoom dashboards. Twilio callback URLs are set per-call by the agent.

## Security posture

- All API keys/certificates live in **Secret Manager only** and are injected
  into Cloud Run via `--set-secrets`; nothing is committed or baked into
  images.
- Agent services are **not** publicly invokable; only the runtime SA
  (orchestrator A2A calls, Cloud Scheduler OIDC) can reach them.
- Webhook receivers are public but verify Mailgun/Twilio/Zoom signatures on
  every request.
- Containers run as `nobody` in Cloud Run's gVisor sandbox.
- Optional: connect the project to a cloud-security scanner (e.g. Wiz) for
  continuous posture/vulnerability monitoring — see docs/PREREQUISITES.md.

`infra/terraform/` stays reserved for future state-managed IaC if/when these
scripts need replacing.
