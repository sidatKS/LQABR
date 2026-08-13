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
