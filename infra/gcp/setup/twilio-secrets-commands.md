# Twilio secrets — command reference

Exact commands used to create, populate, verify, and rotate the two Twilio
secrets in Google Secret Manager for project `ldqfingsrv`. Companion to
[02-secret-manager.md](02-secret-manager.md).

**Never paste secret values into chat, commits, or this file.** The commands
below read values via hidden input (`read -s`) and pipe them straight to Secret
Manager, so the value never appears on the command line or in shell history.

Secrets covered:

| Secret | Holds |
|---|---|
| `lqabr-twilio-account-sid` | Twilio Account SID (`AC…`, 34 chars) — REST basic-auth username **and** the `/Accounts/{SID}` URL path segment |
| `lqabr-twilio-auth-token` | Twilio auth token — REST basic-auth password **and** the HMAC-SHA1 key for `X-Twilio-Signature` webhook validation |

> **The auth token does double duty.** Unlike Mailgun — which has a *separate*
> `lqabr-mailgun-webhook-signing-key` — Twilio signs inbound webhooks with the
> same auth token used for outbound API calls (`validate_twilio_signature()` in
> `agents/text_voice/src/twilio_client.py`). One secret, two blast radii: see
> [Rotation caveat](#rotation-caveat).

> `TWILIO_FROM_NUMBER` (E.164) is **config, not a secret** — it lives in
> `config.sh` and is set at step 05, not here. Same for `LQABR_WEBHOOK_BASE_URL`.

## Prerequisites

```bash
gcloud auth login                       # active account = swaroop@ (CLI creds)
export PROJECT=ldqfingsrv
```

## Create + set value (first time)

Run each pair together — `create` the container, then add version 1.

```bash
# 1) Twilio Account SID
gcloud secrets create lqabr-twilio-account-sid \
  --replication-policy=automatic --project "$PROJECT"
read -r -s -p "Twilio Account SID: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-twilio-account-sid --data-file=- --project "$PROJECT"; unset V; echo

# 2) Twilio auth token
gcloud secrets create lqabr-twilio-auth-token \
  --replication-policy=automatic --project "$PROJECT"
read -r -s -p "Twilio auth token: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-twilio-auth-token --data-file=- --project "$PROJECT"; unset V; echo
```

Expected: `Created secret [...]` then `Created version [1] of the secret [...]`.

## Rotate (after a Twilio auth-token rotation)

The containers already exist — **skip `create`**, just add a new version. The
new version becomes `latest`; services picking up `:latest` get it on next
deploy/restart.

```bash
read -r -s -p "New Twilio auth token: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-twilio-auth-token --data-file=- --project "$PROJECT"; unset V; echo
```

### Rotation caveat

Because the auth token is *also* the webhook signing key, rotating it breaks
**both** directions until the text_voice services restart with the new `:latest`:

- Outbound SMS/calls fail `401` from the Twilio REST API.
- Inbound Twilio callbacks fail `401 invalid Twilio signature` at
  `lqabr-text-voice-webhook`, because the running container still HMACs with the
  old token.

Twilio supports a primary/secondary token during rotation — promote the new
token in the Twilio console, then add the version here and redeploy
(`05_deploy_agents.sh`) promptly so the two sides converge.

## Verify (metadata only — never prints the value)

```bash
# secrets exist?
gcloud secrets list --project "$PROJECT" --filter="name:lqabr-twilio" --format="value(name)"

# versions + state (enabled/disabled/destroyed)
gcloud secrets versions list lqabr-twilio-account-sid --project "$PROJECT" \
  --format="table(name, state, createTime)"
gcloud secrets versions list lqabr-twilio-auth-token --project "$PROJECT" \
  --format="table(name, state, createTime)"
```

Verified 2026-07-16: both secrets present, version `1` `enabled`
(`account-sid` 09:35:23, `auth-token` 09:36:24 UTC).

## Read back a value (only when debugging — avoid; prints plaintext)

```bash
# Prints the secret to your terminal — do NOT run where it could be logged/shared.
gcloud secrets versions access latest --secret=lqabr-twilio-auth-token --project "$PROJECT"
```

## Live credential check (optional — does not print the secrets)

Confirms the stored SID/token pair actually authenticates against Twilio. Prints
only the HTTP status: `200` = good, `401` = wrong SID or token.

```bash
SID=$(gcloud secrets versions access latest --secret=lqabr-twilio-account-sid --project "$PROJECT")
TOK=$(gcloud secrets versions access latest --secret=lqabr-twilio-auth-token --project "$PROJECT")
curl -s -o /dev/null -w '%{http_code}\n' -u "$SID:$TOK" \
  "https://api.twilio.com/2010-04-01/Accounts/$SID.json"
unset SID TOK
```

## How these reach the agent

Step `05_deploy_agents.sh` injects them into the text_voice services:

```
--set-secrets LQABR_TWILIO_ACCOUNT_SID=lqabr-twilio-account-sid:latest
--set-secrets LQABR_TWILIO_AUTH_TOKEN=lqabr-twilio-auth-token:latest
```

`TwilioClient` reads them through `lqabr_core.secrets.get_secret()`, which
prefers the env var and falls back to a Secret Manager fetch. Locally, the same
names upper-cased go in a git-ignored `agents/text_voice/.env`.

## Who can read these

- Runtime service account `lqabr-agent-runtime@…` — `secretmanager.secretAccessor`
  (granted in `01`); the deployed Text/Voice agent + webhook read them at runtime.
- Dev group `ai2d@aidefinitive.com` — `secretmanager.secretAccessor`; their code
  can read the values. See [access-developer-group.md](access-developer-group.md).
