# 02 — Secret Manager (DEV)

Script: `02_secret_manager.sh` · Environment: **dev** (`ldqfingsrv-dev`) · Run 2026-07-21 · **done** (6/12 populated)

Secret **values** are entered directly at the hidden prompt and are **never**
pasted into chat, committed, or recorded here. This doc tracks only which
secret *names* exist and whether they hold a version.

## What it does

Loops over `LQABR_SECRETS`, creating each secret container and prompting
(hidden input) for a value. Empty input → container created with no version.
Idempotent (existing secrets are skipped).

## Command

```bash
cd infra/gcp
( source ./config.dev.sh && source ./02_secret_manager.sh )
```

## Progress (metadata only)

| Secret | Container | Version | Notes |
|---|---|---|---|
| `lqabr-anthropic-api-key` | ✅ | v1 | Anthropic API key (`sk-ant-…`) — models use Claude, not Gemini (Path A) |
| `lqabr-hubspot-access-token` | ✅ | v1 | HubSpot private-app token |
| `lqabr-mailgun-api-key` | ✅ | v1 | Mailgun sending API key |
| `lqabr-mailgun-webhook-signing-key` | ✅ | v1 | Mailgun HTTP webhook signing key |
| `lqabr-twilio-account-sid` | ✅ | v1 | Twilio Account SID (`AC…`) |
| `lqabr-twilio-auth-token` | ✅ | v1 | Twilio auth token + `X-Twilio-Signature` HMAC key |
| `lqabr-zoominfo-username` | ✅ | — | ⏸ empty — SSO blocker (see below) |
| `lqabr-zoominfo-password` | ✅ | — | ⏸ empty — SSO blocker |
| `lqabr-zoom-account-id` | ✅ | — | ⏸ empty — no dev developer access (see below) |
| `lqabr-zoom-client-id` | ✅ | — | ⏸ empty — no dev developer access |
| `lqabr-zoom-client-secret` | ✅ | — | ⏸ empty — no dev developer access |
| `lqabr-zoom-webhook-secret-token` | ✅ | — | ⏸ empty — no dev developer access |

Add a value to any empty secret later with:

```bash
printf '%s' 'THE-VALUE' | gcloud secrets versions add <name> --data-file=- --project ldqfingsrv-dev
```

## Model provider change (Gemini → Anthropic)

Dev uses **Claude/Anthropic** models, not Gemini, so the secret list swapped
`lqabr-google-api-key` → **`lqabr-anthropic-api-key`** (Path A: Anthropic API
directly). The google secret was auto-created on the first pass and deleted
before the real run. **Code change still pending:** agents currently pass a bare
model string (`model=MODEL`), which only works for Gemini; using Claude needs a
shared `build_model()` helper wrapping non-Gemini models in ADK's `LiteLlm`,
plus `litellm` in each `requirements.txt`. Tracked separately.

## ZoomInfo — parked (SSO blocker)

Org is SSO-federated; `zoominfo_client.py` authenticates with raw
username/password, which an SSO login doesn't have. Unblock via either a
dedicated API-entitled, SSO-exempt service account (fills the two secrets as-is)
or PKI auth (client-id + private key, code change). Skipped for dev — ingestion
can use the CSV source meanwhile.

## Zoom — parked (no developer access)

Zoom Workplace account has SSO; the four Zoom secrets come from a **Server-to-
Server OAuth app** (Marketplace → Develop → Build App), which is independent of
SSO but requires the developer role. The "Develop" menu isn't visible for this
account, so an admin must grant the Server-to-Server OAuth privilege or create
the app and hand over Account ID / Client ID / Client Secret / Secret Token.
Skipped for dev — scheduling is the final stage (leads ≥ 60) and not needed yet.

## Gotchas

- **`LQABR_SECRETS: unbound variable`** when run as `bash 02_secret_manager.sh`:
  bash arrays are **not exported to child processes**, so the child script never
  saw the array. Fix: run it **sourced** in a subshell so it executes in a shell
  that already has the array — `( source ./config.dev.sh && source ./02_secret_manager.sh )`.
  The subshell keeps the script's `set -e` from affecting the interactive shell.
  (Scripts 05/06 use arrays too — same fix, or patch scripts to self-source config.)
- `versions add` fails `NOT_FOUND` if the `create` step didn't run first — the
  script always creates the container before prompting.
# 02 — Secret Manager

Script: `02_secret_manager.sh` · **in progress** (5/11 populated) · started 2026-07-16

Secret **values** are entered by the owner directly into Secret Manager and are
**never** pasted into chat, committed, or recorded here. This doc tracks only
which secret *names* exist and whether they hold a version.

## Method

Done per-secret (not the full interactive script) so values can be added as they
become available. Secure pattern — hidden input, value never on the command line
or in shell history:

```bash
gcloud secrets create <name> --replication-policy=automatic --project ldqfingsrv
read -r -s -p "<name>: " V && printf '%s' "$V" | \
  gcloud secrets versions add <name> --data-file=- --project ldqfingsrv; unset V; echo
```

## Progress (metadata only)

| Secret | Container | Version | Notes |
|---|---|---|---|
| `lqabr-mailgun-api-key` | ✅ | v1 enabled (2026-07-16) | Mailgun sending API key (`auth=("api", KEY)`) |
| `lqabr-mailgun-webhook-signing-key` | ✅ | v1 enabled (2026-07-16) | Mailgun HTTP webhook signing key |
| `lqabr-hubspot-access-token` | ✅ | v1 enabled (2026-07-16) | HubSpot Service Key "ZinchMarketingAgent" (`pat-na2-…`). Scopes confirmed 2026-07-16: `crm.schemas.contacts.write`, `crm.objects.contacts.read/write` present (+leads/deals/companies/appointments/lists) → step 04 ready |
| `lqabr-twilio-account-sid` | ✅ | v1 enabled (2026-07-16) | Twilio Account SID (`AC…`) — REST basic-auth username + `/Accounts/{SID}` path |
| `lqabr-twilio-auth-token` | ✅ | v1 enabled (2026-07-16) | Twilio auth token — REST basic-auth password **and** the `X-Twilio-Signature` HMAC key (no separate webhook secret, unlike Mailgun) |
| `lqabr-zoominfo-username` | — | — | ⏸ **parked** — org uses SSO (see note) |
| `lqabr-zoominfo-password` | — | — | ⏸ **parked** — org uses SSO (see note) |
| `lqabr-zoom-account-id` | — | — | pending |
| `lqabr-zoom-client-id` | — | — | pending |
| `lqabr-zoom-client-secret` | — | — | pending |
| `lqabr-zoom-webhook-secret-token` | — | — | pending |

## Related non-secret config (for step 05, NOT Secret Manager)

From the Mailgun setup (`reply.tekninjas.com`, US region `api.mailgun.net`):

- `MAILGUN_DOMAIN=reply.tekninjas.com`
- `MAILGUN_FROM="LQABR Outreach <outreach@reply.tekninjas.com>"` (confirm sender)

Mailgun **verification public key** (`pubkey-…`) was provided but is **not used**
by LQABR (email-validation API) — no secret stored for it.

From the Twilio setup — both still hold `config.sh` placeholders:

- `TWILIO_FROM_NUMBER` — the E.164 sending number. **Not yet set**; `config.sh`
  has `+15551234567`. `TwilioClient.__init__` raises
  `TwilioError: TWILIO_FROM_NUMBER is not configured` without it, so the two
  Twilio secrets alone are not enough to make the agent work.
- `LQABR_WEBHOOK_BASE_URL` — public base URL of the text_voice webhook service
  (the Cloud Run URL after `05`; an ngrok tunnel for local Twilio testing).

## ZoomInfo — parked (SSO blocker)

The ZoomInfo org is SSO-federated (`zoominfo.tekninjas.com` → SAML →
`app.zoominfo.com`). LQABR's `zoominfo_client.py` authenticates by POSTing raw
`username`/`password` to `https://api.zoominfo.com/authenticate` — an SSO login
has no ZoomInfo-native password, so that flow fails. Unblock via **either**:

- **Path A (no code change):** a dedicated ZoomInfo **API service account** that
  is API-entitled **and** exempt from SSO (own username/password) → store in the
  two secrets as-is. Request from ZoomInfo admin / CSM.
- **Path B (code change):** switch to ZoomInfo **PKI auth** (client-id + private
  key) in `agents/ingestion/src/zoominfo_client.py` → different secret shape.

## Gotcha

- `versions add` fails `NOT_FOUND` if the `create` step didn't run first — create
  the container before adding a version.

## Per-vendor command references

- Mailgun: [mailgun-secrets-commands.md](mailgun-secrets-commands.md) — create,
  set, rotate, verify commands for the two Mailgun secrets.
- HubSpot: [hubspot-secrets-commands.md](hubspot-secrets-commands.md) — create,
  set, rotate, verify commands + the token's confirmed scopes.
