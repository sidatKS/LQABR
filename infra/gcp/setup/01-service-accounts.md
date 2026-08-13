# 01 — runtime service account + roles

Script: `01_service_accounts.sh` · Run 2026-07-15 · **done**

## What it does

Creates the agent runtime service account and binds its least-privilege
runtime roles. Idempotent (`describe || create`, bindings are add-only). This
SA is the identity every Cloud Run service runs as, and is the **trust
boundary** — it stays owner-managed; the dev group is never granted the IAM-admin
needed to re-run this.

## Command

```bash
# env-override context as in config.md
bash 01_service_accounts.sh
```

## Output / verification observed

SA created: `lqabr-agent-runtime@ldqfingsrv.iam.gserviceaccount.com`
("LQABR agent runtime"). Roles bound (verified):

```
roles/secretmanager.secretAccessor
roles/pubsub.publisher
roles/pubsub.subscriber
roles/run.invoker
roles/aiplatform.user
roles/logging.logWriter
roles/monitoring.metricWriter
```

## Notes

- `secretmanager.secretAccessor` is granted to **all** secrets. Optional future
  hardening: scope to per-secret bindings (this SA is the ceiling of the dev
  group's `iam.serviceAccountUser` actAs grant — see
  [access-developer-group.md](access-developer-group.md)).
- With the SA now existing, the dev group's scoped `iam.serviceAccountUser`
  binding can be applied.
