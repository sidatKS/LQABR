# Developer access — group `ai2d@aidefinitive.com` (DEV)

How the AIDeveloper team (Google Group `ai2d@aidefinitive.com`, display name
"AIDeveloper") is granted access to **operate and redeploy** the LQABR services
on the **dev** project `ldqfingsrv-dev`.

**Model:** infra is provisioned by the owner (`swaroop@`, scripts `00`–`06`
sourced from `config.dev.sh`). Developers do **not** provision infra — they log
in with their own `@aidefinitive.com` account, redeploy app/agent code, and read
secrets. Roles are granted to the **group**, never to individuals.

**Never record secret values here — names and metadata only.**

## Group membership (verified 2026-07-21)

Queried via `gcloud identity groups memberships list --group-email=ai2d@aidefinitive.com`
(required enabling `cloudidentity.googleapis.com` on dev — a 9th API beyond the 8
from step 00):

| Member | Role in group |
|---|---|
| `aidcld@aidefinitive.com` | MEMBER (shared developer login) |
| `swaroop@aidefinitive.com` | OWNER + MEMBER |

## Granted — applied 2026-07-21 (verified in project IAM policy)

Member `group:ai2d@aidefinitive.com`, all project bindings `--condition=None`.

| Role | Scope | Why |
|---|---|---|
| `roles/run.developer` | project `ldqfingsrv-dev` | redeploy revisions to existing Cloud Run services |
| `roles/cloudbuild.builds.editor` | project | `gcloud builds submit` image builds |
| `roles/artifactregistry.writer` | project | push/pull agent images |
| `roles/secretmanager.viewer` | project | **list/see** secret names + metadata (dev-only addition) |
| `roles/secretmanager.secretAccessor` | project | **read** secret values |
| `roles/logging.viewer` | project | read service logs |
| `roles/monitoring.viewer` | project | read metrics |
| `roles/serviceusage.serviceUsageConsumer` | project | use enabled APIs / quota |
| `roles/iam.serviceAccountUser` | SA `lqabr-agent-dev` only | deploy Cloud Run as the runtime SA |

Commands used:

```bash
GROUP=group:ai2d@aidefinitive.com
PROJECT=ldqfingsrv-dev
for role in roles/run.developer roles/cloudbuild.builds.editor \
  roles/artifactregistry.writer roles/secretmanager.viewer \
  roles/secretmanager.secretAccessor roles/logging.viewer \
  roles/monitoring.viewer roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="$GROUP" --role="$role" --condition=None --quiet
done
gcloud iam service-accounts add-iam-policy-binding \
  lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com \
  --member="$GROUP" --role="roles/iam.serviceAccountUser" \
  --project ldqfingsrv-dev --condition=None
```

## Difference from prod (why dev added `secretmanager.viewer`)

Prod granted only `secretmanager.secretAccessor`, which permits reading a value
if you know the exact name but **not** `gcloud secrets list` (that needs
`secrets.list`, in `viewer`). Dev adds `secretmanager.viewer` so developers can
**see the dev secret names before using them** — the original requirement. A dev
in the group can now verify with:

```bash
gcloud secrets list --project ldqfingsrv-dev
```

## Deliberately withheld (least-privilege)

- **No** `owner`/`editor`, no `run.admin`.
- **No** provisioning admin (`pubsub.admin`, `cloudscheduler.admin`,
  `secretmanager.admin`) — developers don't provision.
- **No** `resourcemanager.projectIamAdmin` / `iam.serviceAccountAdmin` — the
  SA + role bootstrap (`01`) stays with the owner (the trust boundary).

## Deferred

- **Cloud Build staging bucket grant:** `roles/storage.objectAdmin` on
  `gs://ldqfingsrv-dev_cloudbuild` — that bucket doesn't exist until the first
  build (step 05); add it then.

## Note on shared login

All developers currently authenticate as the single shared `aidcld@` identity,
so per-developer audit attribution isn't possible; actions appear as `aidcld@`
(or the `lqabr-agent-dev` SA for deployed code). Acceptable for dev; revisit if
individual attribution is needed.
