# Mailgun secrets — command reference

Exact commands used to create, populate, verify, and rotate the two Mailgun
secrets in Google Secret Manager for project `ldqfingsrv`. Companion to
[02-secret-manager.md](02-secret-manager.md).

**Never paste secret values into chat, commits, or this file.** The commands
below read values via hidden input (`read -s`) and pipe them straight to Secret
Manager, so the value never appears on the command line or in shell history.

Secrets covered:

| Secret | Holds |
|---|---|
| `lqabr-mailgun-api-key` | Mailgun sending API key (`auth=("api", KEY)`) |
| `lqabr-mailgun-webhook-signing-key` | Mailgun HTTP webhook signing key |

> `MAILGUN_DOMAIN=reply.tekninjas.com` is **config, not a secret** — set at step
> 05, not here. The Mailgun `pubkey-…` verification key is unused by LQABR.

## Prerequisites

```bash
gcloud auth login                       # active account = swaroop@ (CLI creds)
export PROJECT=ldqfingsrv
```

## Create + set value (first time)

Run each pair together — `create` the container, then add version 1.

```bash
# 1) Mailgun API key
gcloud secrets create lqabr-mailgun-api-key \
  --replication-policy=automatic --project "$PROJECT"
read -r -s -p "Mailgun API key: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-mailgun-api-key --data-file=- --project "$PROJECT"; unset V; echo

# 2) Mailgun webhook signing key
gcloud secrets create lqabr-mailgun-webhook-signing-key \
  --replication-policy=automatic --project "$PROJECT"
read -r -s -p "Mailgun webhook signing key: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-mailgun-webhook-signing-key --data-file=- --project "$PROJECT"; unset V; echo
```

Expected: `Created secret [...]` then `Created version [1] of the secret [...]`.

## Rotate (add a new value later)

The container already exists — **skip `create`**, just add a new version. The
new version becomes `latest`; services picking up `:latest` get it on next
deploy/restart.

```bash
read -r -s -p "New Mailgun API key: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-mailgun-api-key --data-file=- --project "$PROJECT"; unset V; echo
```

## Verify (metadata only — never prints the value)

```bash
# secrets exist?
gcloud secrets list --project "$PROJECT" --filter="name:lqabr-mailgun" --format="value(name)"

# versions + state (enabled/disabled/destroyed)
gcloud secrets versions list lqabr-mailgun-api-key --project "$PROJECT" \
  --format="table(name, state, createTime)"
gcloud secrets versions list lqabr-mailgun-webhook-signing-key --project "$PROJECT" \
  --format="table(name, state, createTime)"
```

Verified 2026-07-16: both secrets present, version `1` `enabled`.

## Read back a value (only when debugging — avoid; prints plaintext)

```bash
# Prints the secret to your terminal — do NOT run where it could be logged/shared.
gcloud secrets versions access latest --secret=lqabr-mailgun-api-key --project "$PROJECT"
```

## Who can read these

- Runtime service account `lqabr-agent-runtime@…` — `secretmanager.secretAccessor`
  (granted in `01`); the deployed Email agent/webhook read them at runtime.
- Dev group `ai2d@aidefinitive.com` — `secretmanager.secretAccessor`; their code
  can read the values. See [access-developer-group.md](access-developer-group.md).
# Mailgun secrets — command reference

Exact commands used to create, populate, verify, and rotate the two Mailgun
secrets in Google Secret Manager for project `ldqfingsrv`. Companion to
[02-secret-manager.md](02-secret-manager.md).

**Never paste secret values into chat, commits, or this file.** The commands
below read values via hidden input (`read -s`) and pipe them straight to Secret
Manager, so the value never appears on the command line or in shell history.

Secrets covered:

| Secret | Holds |
|---|---|
| `lqabr-mailgun-api-key` | Mailgun sending API key (`auth=("api", KEY)`) |
| `lqabr-mailgun-webhook-signing-key` | Mailgun HTTP webhook signing key |

> `MAILGUN_DOMAIN=reply.tekninjas.com` is **config, not a secret** — set at step
> 05, not here. The Mailgun `pubkey-…` verification key is unused by LQABR.

## Prerequisites

```bash
gcloud auth login                       # active account = swaroop@ (CLI creds)
export PROJECT=ldqfingsrv
```

## Create + set value (first time)

Run each pair together — `create` the container, then add version 1.

```bash
# 1) Mailgun API key
gcloud secrets create lqabr-mailgun-api-key \
  --replication-policy=automatic --project "$PROJECT"
read -r -s -p "Mailgun API key: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-mailgun-api-key --data-file=- --project "$PROJECT"; unset V; echo

# 2) Mailgun webhook signing key
gcloud secrets create lqabr-mailgun-webhook-signing-key \
  --replication-policy=automatic --project "$PROJECT"
read -r -s -p "Mailgun webhook signing key: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-mailgun-webhook-signing-key --data-file=- --project "$PROJECT"; unset V; echo
```

Expected: `Created secret [...]` then `Created version [1] of the secret [...]`.

## Rotate (add a new value later)

The container already exists — **skip `create`**, just add a new version. The
new version becomes `latest`; services picking up `:latest` get it on next
deploy/restart.

```bash
read -r -s -p "New Mailgun API key: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-mailgun-api-key --data-file=- --project "$PROJECT"; unset V; echo
```

## Verify (metadata only — never prints the value)

```bash
# secrets exist?
gcloud secrets list --project "$PROJECT" --filter="name:lqabr-mailgun" --format="value(name)"

# versions + state (enabled/disabled/destroyed)
gcloud secrets versions list lqabr-mailgun-api-key --project "$PROJECT" \
  --format="table(name, state, createTime)"
gcloud secrets versions list lqabr-mailgun-webhook-signing-key --project "$PROJECT" \
  --format="table(name, state, createTime)"
```

Verified 2026-07-16: both secrets present, version `1` `enabled`.

## Read back a value (only when debugging — avoid; prints plaintext)

```bash
# Prints the secret to your terminal — do NOT run where it could be logged/shared.
gcloud secrets versions access latest --secret=lqabr-mailgun-api-key --project "$PROJECT"
```

## Who can read these

- Runtime service account `lqabr-agent-runtime@…` — `secretmanager.secretAccessor`
  (granted in `01`); the deployed Email agent/webhook read them at runtime.
- Dev group `ai2d@aidefinitive.com` — `secretmanager.secretAccessor`; their code
  can read the values. See [access-developer-group.md](access-developer-group.md).
