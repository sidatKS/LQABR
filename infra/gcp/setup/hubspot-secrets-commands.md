# HubSpot secret — command reference

Exact commands used to create, populate, verify, and rotate the HubSpot access
token secret in Google Secret Manager for project `ldqfingsrv`, plus the token's
scopes. Companion to [02-secret-manager.md](02-secret-manager.md).

**Never paste the token into chat, commits, or this file.** The commands below
read the value via hidden input (`read -s`) and pipe it straight to Secret
Manager, so it never appears on the command line or in shell history.

Secret covered:

| Secret | Holds |
|---|---|
| `lqabr-hubspot-access-token` | HubSpot Service Key "ZinchMarketingAgent" (`pat-na2-…`), used by `lqabr_core.crm.HubSpotClient` for all CRM API calls |

## Prerequisites

```bash
gcloud auth login                       # active account = swaroop@ (CLI creds)
export PROJECT=ldqfingsrv
```

## Create + set value (first time)

```bash
gcloud secrets create lqabr-hubspot-access-token \
  --replication-policy=automatic --project "$PROJECT"

read -r -s -p "HubSpot access token: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-hubspot-access-token --data-file=- --project "$PROJECT"; unset V; echo
```

Expected: `Created secret [lqabr-hubspot-access-token].` then
`Created version [1] of the secret [...]`.

## Rotate (after a HubSpot key "Rotate")

Container already exists — **skip `create`**, add a new version; it becomes
`latest`. Redeploy/restart services to pick it up.

```bash
read -r -s -p "New HubSpot access token: " V && printf '%s' "$V" | \
  gcloud secrets versions add lqabr-hubspot-access-token --data-file=- --project "$PROJECT"; unset V; echo
```

## Verify (metadata only — never prints the token)

```bash
gcloud secrets list --project "$PROJECT" --filter="name:lqabr-hubspot" --format="value(name)"
gcloud secrets versions list lqabr-hubspot-access-token --project "$PROJECT" \
  --format="table(name, state, createTime)"
```

Verified 2026-07-16: version `1` `enabled`.

## Read back (debug only — prints plaintext, avoid)

```bash
gcloud secrets versions access latest --secret=lqabr-hubspot-access-token --project "$PROJECT"
```

## Token scopes (confirmed 2026-07-16)

Service Key **ZinchMarketingAgent**. **Required by LQABR** marked ✅ (step `04`
creates the `lqabr_*` contact properties; agents upsert contacts at runtime):

| Scope | LQABR use |
|---|---|
| `crm.schemas.contacts.write` | ✅ **required** — create/modify `lqabr_*` contact property definitions (step 04) |
| `crm.objects.contacts.read` | ✅ **required** — read contacts |
| `crm.objects.contacts.write` | ✅ **required** — upsert lead state onto contacts |
| `crm.objects.leads.read` / `.write` | extra (not required) |
| `crm.objects.deals.read` / `.write` | extra |
| `crm.objects.companies.read` / `.write` | extra |
| `crm.objects.appointments.read` / `.write` | extra |
| `crm.objects.owners.read` | extra |
| `crm.lists.read` / `.write` | extra |
| `sales-email-read` | extra |

Extras are broader than strictly needed but harmless. Minimum viable set for
LQABR is the three ✅ scopes; if tightening later, keep those.

## Who can read this secret

- Runtime SA `lqabr-agent-runtime@…` — `secretmanager.secretAccessor` (from `01`).
- Dev group `ai2d@aidefinitive.com` — `secretmanager.secretAccessor`
  (see [access-developer-group.md](access-developer-group.md)).
