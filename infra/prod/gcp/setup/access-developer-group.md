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
# Developer access — group `ai2d@aidefinitive.com`

How the AIDeveloper team (Google Group `ai2d@aidefinitive.com`, display name
"AIDeveloper", contains `aidcld@aidefinitive.com`) is granted access to
**operate and redeploy** the LQABR services on project `ldqfingsrv`.

**Model:** infra is provisioned **once** by the project owner (`swaroop@`,
scripts `00`–`06`). Developers do **not** provision infra — they install gcloud
locally, `init`/`auth`/`config` with their own `aidcld@` login (see
[prerequisites.md](prerequisites.md)), then redeploy app/agent code and read
secrets *from their code*. Roles are granted to the **group**, never to
individuals — membership is managed in Workspace.

**Never record secret values here — names and metadata only.**

## Design protocol

This was a Tier-3 IAM change run through the `design-protocol` skill:
ground → propose → `architect-critic` (returned REVISE; dropped every `*.admin`
role that carries `setIamPolicy`, scoped `serviceAccountUser`/storage to single
resources) → deterministic check → `cost-estimator` (N/A, bindings are free) →
explicit owner approval. The AACLIP pre-commit gate additionally **blocked**
`roles/run.admin` at project scope, confirming the downgrade to
`roles/run.developer`.

## Granted — applied 2026-07-15 (verified)

Member `group:ai2d@aidefinitive.com`, all project bindings `--condition=None`.

| Role | Scope | Why |
|---|---|---|
| `roles/run.developer` | project `ldqfingsrv` | redeploy revisions to existing Cloud Run services |
| `roles/cloudbuild.builds.editor` | project | `gcloud builds submit` image builds |
| `roles/artifactregistry.writer` | project | push/pull agent images |
| `roles/secretmanager.secretAccessor` | project | deployed code reads secret values |
| `roles/logging.viewer` | project | read service logs |
| `roles/monitoring.viewer` | project | read metrics |
| `roles/serviceusage.serviceUsageConsumer` | project | use enabled APIs / quota |
| `roles/billing.viewer` | billing acct `015999-2B94BB-C053F1` | cost visibility |

Commands used (per role):

```bash
gcloud projects add-iam-policy-binding ldqfingsrv \
  --member="group:ai2d@aidefinitive.com" --role="<role>" --condition=None
gcloud billing accounts add-iam-policy-binding 015999-2B94BB-C053F1 \
  --member="group:ai2d@aidefinitive.com" --role="roles/billing.viewer"
```

## Resource-scoped bindings — applied 2026-07-15 (verified)

Applied after `01` created the runtime SA and the `_cloudbuild` bucket was
pre-created (see [01-service-accounts.md](01-service-accounts.md)).

```bash
# actAs ONLY the runtime SA (needed to deploy Cloud Run as it)
gcloud iam service-accounts add-iam-policy-binding \
  lqabr-agent-runtime@ldqfingsrv.iam.gserviceaccount.com \
  --member="group:ai2d@aidefinitive.com" --role="roles/iam.serviceAccountUser" \
  --project ldqfingsrv --condition=None

# write build source to ONLY the Cloud Build staging bucket
gcloud storage buckets add-iam-policy-binding gs://ldqfingsrv_cloudbuild \
  --member="group:ai2d@aidefinitive.com" --role="roles/storage.objectAdmin"
```

Both verified present. `storage.objectAdmin` passed the AACLIP gate because it
is bound at **bucket** scope (a specific resource), not project scope. The dev
group now holds all 10 bindings — access is complete.

## Deliberately withheld (least-privilege)

- **No** `owner`/`editor`, **no** project-scope `run.admin` (blocked by the
  AACLIP gate — carries `setIamPolicy` = public-exposure lever).
- **No** provisioning admin (`pubsub.admin`, `cloudscheduler.admin`,
  `secretmanager.admin`, project `storage.admin`) — developers don't provision.
- **No** `resourcemanager.projectIamAdmin` / `iam.serviceAccountAdmin` — the
  one-time SA + role bootstrap (`01`) stays with the owner (the trust boundary).

## Implications / gotchas

- **Public exposure is owner-only.** `run.developer` cannot flip a service
  public/private, so developer redeploys must **not** re-apply
  `--allow-unauthenticated`. The public invoker binding on the 3 webhook
  services is a one-time owner action during infra setup. Script `05` as written
  re-applies the flag on every deploy — for developer redeploys either run a
  plain `gcloud run deploy --image ...` (no invoker flag) or split that step.
- **`secretmanager.secretAccessor` is project-wide** — any group member's code
  can read **every** `lqabr-*` secret value. Accepted by the owner. To tighten
  later, replace with per-secret `add-iam-policy-binding` on individual secrets.
- **Runtime-SA ceiling (optional hardening):** deployed code runs as the runtime
  SA, which `01` grants `secretmanager.secretAccessor` to all secrets. Scoping
  the SA to per-secret access would tighten the deploy-as path. Separate `01`
  edit, not yet done.

## Revert

Snapshot before grants: `scratchpad/ldqfingsrv-iam-before.json` (only
`swaroop@ = roles/owner`). To undo any binding:

```bash
gcloud projects remove-iam-policy-binding ldqfingsrv \
  --member="group:ai2d@aidefinitive.com" --role="<role>" --condition=None
```
