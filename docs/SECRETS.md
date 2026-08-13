# Secrets — Google Secret Manager at runtime

Context §7.6 and CLAUDE.md §5: *secrets come from Secret Manager only, never
hard-coded, never committed*. Mahi, 29 Jul: **"we can't use as an environment
variable"**, **"never hard-code"**.

This is now literally true. No credential is read from the process environment
in production.

## How it works

`lqabr_core/leadgen/secrets.py` resolves a **logical name** (`ANTHROPIC_API_KEY`) to a Secret
Manager **resource** (`projects/<p>/secrets/<id>/versions/latest`), fetches it
over the API using the runtime service account's own identity, caches it in
memory for the TTL, and returns it. Nothing is written to disk and nothing is
written to `os.environ`.

```
LQABR_SECRET_<LOGICAL>   ->  which Secret Manager entry            (non-secret)
LQABR_SECRET_PROJECT     ->  which GCP project                      (non-secret)
LQABR_SECRET_BACKEND     ->  gcp (default) | env (local dev / CI)   (non-secret)
LQABR_SECRET_TTL_SECONDS ->  in-process cache TTL, default 900      (non-secret)
```

Everything the repo configures is a **name**, never a value — which is why the
`.env.example` files are safe to commit and read.

## The five secrets

| logical name | used by | default entry id |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | the orchestrating model | `lqabr-anthropic-api-key` |
| `HUBSPOT_PRIVATE_APP_TOKEN` | auth, interim mode | `lqabr-hubspot-private-app-token` |
| `HUBSPOT_CLIENT_ID` | auth, target mode | `lqabr-hubspot-client-id` |
| `HUBSPOT_CLIENT_SECRET` | auth, target mode | `lqabr-hubspot-client-secret` |
| `HUBSPOT_REFRESH_TOKEN` | auth, target mode | `lqabr-hubspot-refresh-token` |

Point any of them at an existing entry without renaming anything in GCP:

```
LQABR_SECRET_ANTHROPIC_API_KEY=whatever-you-already-called-it
# or pin a version:
LQABR_SECRET_HUBSPOT_REFRESH_TOKEN=projects/my-proj/secrets/hs-refresh/versions/7
```

## Four properties worth knowing

**It fails closed.** The backend defaults to `gcp` and there is *no* silent
fallback to an environment variable — the same reasoning as `HUBSPOT_AUTH_MODE`.
A deploy missing `LQABR_SECRET_PROJECT` raises rather than quietly running on
whatever happened to be in the environment. `LQABR_SECRET_BACKEND=env` is the
local-dev and CI path and has to be typed deliberately.

**A missing credential halts the run.** `SecretConfigError` and
`SecretAccessError` become `AuthConfigError` → `SystemicFailure`, which stops
the run and writes **nothing** to `errors/schema_mismatch.jsonl`. One missing
secret is not 263 bad leads.

**The value is never logged.** The `secret_resolved` process line and the
`secret_manager_access` audit line record *which* secret, *which* version and
how long it took — the value is redacted to `<48 chars, ends f9c2>`. A test
asserts the raw value appears nowhere in any stream.

**Rotation needs no redeploy.** The version is resolved at runtime, so a new
version is picked up on the next run (or after the TTL in a long-lived
process). One API call per secret per run, not per use.

## Deploying

The runtime service account needs `roles/secretmanager.secretAccessor` on each
secret — grant per secret, not project-wide:

```bash
SA=lqabr-lead-profile@<project>.iam.gserviceaccount.com

for s in lqabr-anthropic-api-key \
         lqabr-hubspot-private-app-token \
         lqabr-hubspot-client-id \
         lqabr-hubspot-client-secret \
         lqabr-hubspot-refresh-token; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done

gcloud run jobs deploy lqabr-lead-profile \
  --image lqabr-lead-profile --region <region> \
  --service-account "$SA" \
  --set-env-vars LQABR_SECRET_PROJECT=<project>,LQABR_AGENT_MODEL=anthropic/claude-sonnet-4-6,HUBSPOT_AUTH_MODE=private_app \
  --add-volume=name=data,type=cloud-storage,bucket=<bucket> \
  --add-volume-mount=volume=data,mount-path=/data
```

Note there is no `--set-secrets`. Cloud Run's secret-to-env-var injection would
put the values back in the environment, which is the thing being avoided.
Workload identity means no key file exists anywhere.

## Locally

```bash
gcloud auth application-default login     # ADC — no key file on disk
export LQABR_SECRET_PROJECT=<project>
adk web agents
```

Your own Google account needs `secretAccessor` on the secrets. If you'd rather
not touch GCP while developing:

```bash
export LQABR_SECRET_BACKEND=env
export ANTHROPIC_API_KEY=...        # this shell only; never in a committed file
```

The test suite runs on the `env` backend and never reaches Secret Manager.

## The lead-generation resolver

`lqabr_core/leadgen/secrets.py` lives in the shared library beside
`lqabr_core/obs/` so the Lead Profile Agent and its MCP server
(`lqabr_core.leadgen.server`) resolve the *same* HubSpot credentials through one
resolver, one set of entries, one audit trail.
`from lqabr_core.leadgen.secrets import get_secret`.

> Note: the other agents use the repo's *other* resolver, `lqabr_core.secrets`
> (env-first, `SecretNotFoundError`). The two coexist as a known reconciliation
> item — this logical-name, fail-closed resolver is the lead_profile path's.
