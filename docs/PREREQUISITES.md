# LQABR Prerequisites

Complete everything here before running any script or agent. Work top to
bottom; each section ends with a verification step.

## 1. Accounts & API credentials

You need an account and an API credential for each service. Collect the
values now — script `infra/gcp/02_secret_manager.sh` will prompt for them
and store them in **Google Secret Manager** (the only place secrets live).

| Service | What to create | Credential(s) | Secret Manager name |
|---|---|---|---|
| **HubSpot** | Private app (Settings → Integrations → Private Apps) with `crm.objects.contacts` read/write and `crm.schemas.contacts` read/write scopes | Access token | `lqabr-hubspot-access-token` |
| **Mailgun** | Account + verified sending domain (add the DNS records Mailgun gives you) | API key; webhook signing key (Settings → Webhooks) | `lqabr-mailgun-api-key`, `lqabr-mailgun-webhook-signing-key` |
| **Twilio** | Account + an SMS/voice-capable phone number | Account SID; Auth token | `lqabr-twilio-account-sid`, `lqabr-twilio-auth-token` |
| **ZoomInfo** | API access on your subscription | API username; password | `lqabr-zoominfo-username`, `lqabr-zoominfo-password` |
| **Zoom** | Server-to-Server OAuth app (marketplace.zoom.us) with Scheduler scopes; a Zoom Scheduler schedule (booking page); an event subscription for Scheduler events | Account ID; Client ID; Client secret; Webhook secret token | `lqabr-zoom-account-id`, `lqabr-zoom-client-id`, `lqabr-zoom-client-secret`, `lqabr-zoom-webhook-secret-token` |
| **Google Cloud** | Project with billing enabled | Your user account (Owner/Editor) | — |
| **Gemini** | AI Studio key (dev) or Vertex AI (gcp envs) | `GOOGLE_API_KEY` / ADC | env/ADC, not Secret Manager |

**Verify:** you can log into each dashboard and have every value in a
password manager (not in a file in this repo).

## 2. Local tooling

| Tool | Min version | Verify |
|---|---|---|
| Python | 3.10+ | `python3 --version` |
| pip | 22+ | `pip --version` |
| gcloud CLI | 450+ | `gcloud --version` |
| git | 2.30+ | `git --version` |

Install the repo's Python dependencies:

```bash
pip install -e packages/lqabr_core            # shared core (all agents need it)
pip install -r agents/lead_profile/requirements.txt   # google-adk + friends
pip install pytest fastapi httpx python-multipart      # test/webhook extras
```

**Verify:** `python3 -m pytest -c tests/pytest.ini -q` from the repo root — all tests pass
without any credentials (external services are mocked).

## 3. GCP authentication & project

```bash
gcloud auth login
gcloud auth application-default login   # ADC for Python clients
gcloud config set project YOUR_PROJECT_ID
```

Requirements on the project: billing enabled; you hold Owner or Editor;
API enablement not blocked by org policy (script 00 enables Run, Cloud
Build, Artifact Registry, Secret Manager, Pub/Sub, Cloud Scheduler,
Vertex AI, IAM).

**Verify:** `gcloud projects describe YOUR_PROJECT_ID` succeeds.

## 4. Provision the platform

Edit `infra/gcp/config.sh` (PROJECT_ID, region, Mailgun domain, Twilio
number, sender name), then run scripts `00` → `06` in order — see
`infra/gcp/README.md` for the full run guide.

**Verify:** the queries/checks at the end of `docs/PHASE0_PLAN.md`.

## 5. Local agent development (ADK web harness)

Each agent runs locally in the ADK dev UI without deploying anything:

```bash
cp agents/<agent>/.env.example agents/<agent>/.env   # fill in real values
adk web agents/<agent>/src                            # browser dev UI
adk run agents/<agent>/src                            # terminal
```

Webhook receivers run locally with uvicorn (see each `webhook_app.py`
header); to receive real Mailgun/Twilio/Zoom events locally you need a
public tunnel (e.g. `ngrok http 8082`) set as `LQABR_WEBHOOK_BASE_URL`.

## 6. Optional but recommended

- **Security posture scanning** (e.g. **Wiz** or Google Security Command
  Center): connect the GCP project for continuous vulnerability and
  misconfiguration monitoring of the Cloud Run services and images.
- **Mailgun test mode / Twilio test credentials** while iterating, so no
  real messages leave the building.
- A dedicated **HubSpot sandbox portal** for dev/stage before pointing at
  production CRM data.

## Common errors

| Symptom | Fix |
|---|---|
| `SecretNotFoundError ... GOOGLE_CLOUD_PROJECT is unset` | Local run without `.env` — copy `.env.example` → `.env` and fill values, or export `GOOGLE_CLOUD_PROJECT` and grant ADC access |
| `Mailgun send failed: HTTP 401` | Wrong API key or unverified domain — check DNS records in Mailgun |
| Twilio webhook 401 `invalid Twilio signature` | `LQABR_WEBHOOK_BASE_URL` doesn't match the public URL Twilio called (scheme/host must match exactly) |
| ZoomInfo `auth failed` | API credentials are separate from portal login — confirm API access is enabled on your subscription |
| HubSpot `403` on property write | Private-app scopes missing `crm.objects.contacts.write` / run `04_hubspot_properties.py` first |
