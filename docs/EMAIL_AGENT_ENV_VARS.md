# Email Agent — every `.env` variable, for Secret Manager

> **Requested by Swaroop, Platform Engineering call 2026-08-04 (3:11):**
> *"List out in every agent that you are going to work. List out anything
> that is in `.env`. I'll have to create or update the existing secret
> manager so that we have all the lists sorted out."*
>
> **Follow-up he asked us to record (3:30):** *"We're going to remove the
> `.env` file dependencies and go with the Secret Manager. That will be in
> the next sprint."*

**Status: done ahead of that.** `agents/email/.env` now holds **no live
credential** — every one resolves through Secret Manager. See §1.

Agent: `agents/email` · Cloud Run service: `lqabr-dev-email-agent` · one image.

---

## 1. Where credentials come from — `LQABR_SECRETS_SOURCE`

| Value | Behaviour |
|---|---|
| `secret_manager` | **The deployed default.** Ignores the environment entirely and always reads the Secret Manager API. A stray literal in a `.env` cannot shadow the real value. Needs `roles/secretmanager.secretAccessor` on the runtime SA, and ADC locally. |
| `auto` | Environment first, then the API. On Cloud Run `--set-secrets` injects Secret Manager values *as* env vars, so this is still Secret Manager — just without an API call per cold start. |
| `env` | Never calls the API. Local work and tests. |

Whichever answers, `lqabr_core.secrets` logs the secret **name** and its source
at INFO — never the value:

```
lqabr.secrets: secret lqabr-hubspot-access-token resolved from Secret Manager (projects/ldqfingsrv-dev)
```

The API path needs `google-cloud-secret-manager`, which the Cloud Run image
installs (`pip install "./packages/lqabr_core[gcp]"`). Without it an
environment variable is the only thing that can resolve a secret — that was
the original defect.

### The secrets themselves

| Secret Manager name | Env var equivalent | Used for | Provisioned? |
|---|---|---|---|
| `lqabr-hubspot-access-token` | `LQABR_HUBSPOT_ACCESS_TOKEN` | HubSpot bearer — steps 4, 5, 9 | yes — **verify the version is the VALID token** |
| `lqabr-mailgun-api-key` | `LQABR_MAILGUN_API_KEY` | Sending (7) and the events tool call (8) | yes |
| `lqabr-mailgun-webhook-signing-key` | `LQABR_MAILGUN_WEBHOOK_SIGNING_KEY` | HMAC on every pushed Mailgun event | yes |
| `lqabr-anthropic-api-key` | `ANTHROPIC_API_KEY` | Step-6 model call when `LQABR_EMAIL_MODEL` is `anthropic/*` | yes (dev) |
| `lqabr-google-api-key` | `GOOGLE_API_KEY` | Step-6 model call for `gemini-*` **on AI Studio**. Not needed on Vertex (`GOOGLE_GENAI_USE_ENTERPRISE=1` → ADC). | yes (prod) |
| `lqabr-hubspot-client-secret` | `LQABR_HUBSPOT_CLIENT_SECRET` | Only when `LQABR_HUBSPOT_AUTH_MODE=oauth2` | not needed today |

**REMOVED 2026-08-05:** `LQABR_EMAIL_GATEWAY_TOKEN` / `lqabr-email-gateway-token`.
The gateway wasn't sending the header, so the check was dropped from
`service_app.py`. `/hubspot/campaign` and `/engagement/sync` are currently
unauthenticated; nothing replaces it yet.

> **Model keys are the subtle one.** litellm and google-genai read their key
> from the *environment* and know nothing about Secret Manager, so a literal
> in `.env` used to be the only way to supply one — and the deploy injected
> none at all. `lqabr_core.model.ensure_provider_credentials()` now resolves
> the provider key through Secret Manager and injects it for the SDK. It is a
> no-op when the key is already set (that is `--set-secrets` working) and when
> Vertex ADC is in use.

### How the HubSpot bearer is obtained — `LQABR_HUBSPOT_AUTH_MODE`

A separate knob, and the one that caused the confusion:

| Value | Behaviour |
|---|---|
| `secret_manager` | **Correct setting.** Reads `lqabr-hubspot-access-token` through `lqabr_core.secrets`. |
| `env` | Reads `LQABR_HUBSPOT_ACCESS_TOKEN` straight from the environment, **bypassing Secret Manager**. This was set in `.env` and is why the token was coming from the file. |
| `oauth2` | Client-credentials grant. Unused today. |

---

## 2. Non-secret configuration

Passed by `05_deploy_agents.sh` as `--set-env-vars`.

| Env var | Default | What it does |
|---|---|---|
| `MAILGUN_DOMAIN` | *(none — raises)* | Sending domain. **Must be set.** |
| `MAILGUN_FROM` | `LQABR <outreach@$MAILGUN_DOMAIN>` | From header |
| `LQABR_SENDER_NAME` | `The LQABR Team` | Signature in the body |
| `LQABR_CTA_URL` | *(empty)* | Tracked call-to-action link. Not fatal — a plain **open** completes the campaign — but an empty CTA costs the click signal. |
| `LQABR_EMAIL_MODEL` | `gemini-2.0-flash` (deploy passes `anthropic/claude-sonnet-5` in dev) | Step-6 model |
| `LQABR_EMAIL_TEMPERATURE` | `1.0` | Anthropic caps at 1.0; the old 1.1 default 400s before a token is generated |
| `LQABR_EMAIL_BATCH_LIMIT` | `25` | Leads per run — what keeps a run inside the request timeout |
| `LQABR_EMAIL_ROUTES` | `all` | One service serves the gateway entry **and** the Mailgun push |
| `LQABR_HUBSPOT_OBJECT_ID_PROPERTY` | `object_id` | Contact property the campaign's leads are chunked under. **Confirm against the live schema** — a wrong name now fails the run. |
| `LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY` | `email_campaign_complete` | Set when `lqabr_email_status` reaches OPENED — the step-10 hand-off |
| `LQABR_EMAIL_ALLOW_QUEUE_FALLBACK` | `0` | Leave off. On, a wrong object-id property silently works the not-yet-emailed queue — i.e. emails a different audience. |
| `LQABR_HUBSPOT_TOKEN_TTL_SECONDS` | `3600` | Bearer cache lifetime per run |
| `LQABR_EMAIL_RUNSTATE_DIR` | `/var/lib/lqabr/email/runstate` | Provisioned in the image and owned by `nobody`; without a writable path every run 503s at step 3 |
| `LQABR_EMAIL_LOG_DIR` | *(unset)* | Tees the four streams to disk; leave unset on Cloud Run (stdout is read) |
| `LQABR_EMAIL_LOG_MODEL_CONTENT` | `0` | Would put prospect PII in `model_log` — keep off |
| `LQABR_EMAIL_MOUNT_ADK` | `0` | Mounts the ADK runner at `/adk` for transition only |
| `GOOGLE_CLOUD_PROJECT` | *(set by deploy)* | **Required** — Secret Manager cannot be queried without it |
| `GOOGLE_GENAI_USE_ENTERPRISE` | `1` (deploy) | Vertex via ADC; `0` uses an AI Studio key |
| `MAILGUN_API_BASE` | `https://api.mailgun.net/v3` | Override for EU-region accounts |
| `PORT` | `8080` | Set by Cloud Run; the app binds `0.0.0.0:$PORT` |

---

## 3. What is left for the next sprint

- **Create `lqabr-email-gateway-token`.** It is the only credential still
  supplied outside Secret Manager. The service is `--allow-unauthenticated`
  (Mailgun must reach it), so this is what protects the entries that spend
  money. The deploy warns and continues without it.
- The `.env` → Secret Manager migration Swaroop scheduled is **already done**
  for this agent; `agents/email/.env` now carries configuration only.

## 4. Still open on the deployed service

- **Run state is per-instance** and does not survive scale-to-zero.
  `POST /engagement/sync` re-pulls track IDs from Mailgun to mitigate it, but a
  durable store (Firestore/GCS) is still needed.
- Deployed by `:latest` rather than by digest, unlike `gtwy` and `txtv`.
- Confirm `object_id` and `email_campaign_complete` against
  `GET /crm/v3/properties/contacts`.
