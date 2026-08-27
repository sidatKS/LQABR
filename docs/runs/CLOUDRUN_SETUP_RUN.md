# LQABR Cloud Run Setup — Execution Tracker

**Source of truth:** `docs/SETUP_CLOUDRUN_IAM.md`, adapted per decisions below.
**Target:** `ldqfingsrv-dev` · `us-central1` · **Runtime SA:** `lqabr-agent-dev` (single)
**Operator:** swaroop@aidefinitive.com (Owner) · **Started:** 2026-08-25

Status: `TODO` · `IN PROGRESS` · `BLOCKED` · `DONE` · `SKIPPED`
Issues found mid-run are recorded as sub-tasks under the phase that surfaced them.

## Decisions taken (user, 2026-08-25)

| # | Decision | Effect |
| --- | --- | --- |
| D1 | Deploy into **`ldqfingsrv-dev`**, not the doc's `leadgen-snbox-11b7c` | Kills the shared-project risk (P0-I1, P0-I11). AR repo + SA + 5 secrets already exist. |
| D2 | **One runtime SA** (`lqabr-agent-dev`), not the doc's six | Accepts today's posture. §2.4 per-secret table collapses to a single project-wide accessor. |
| D3 | **7 secrets** — only what the code reads today | Twilio ×2, Zoom ×4, ZoomInfo ×2 stay dormant (no ingestion/scheduling agent exists). |

---

## P0 — Grounding (read-only) — **DONE**

### `ldqfingsrv-dev` actual state

| Check | Result |
| --- | --- |
| APIs enabled | run, cloudbuild, artifactregistry, secretmanager, logging, monitoring, aiplatform, pubsub, cloudscheduler |
| APIs **missing** | **compute, vpcaccess** (only these two — needed for VPC + NAT) |
| Runtime SA | `lqabr-agent-dev` exists |
| SA project roles | aiplatform.user, logging.logWriter, monitoring.metricWriter, pubsub.publisher, pubsub.subscriber, **run.invoker**, **secretmanager.secretAccessor** (both project-wide) |
| `ai2d@` project roles | artifactregistry.writer, cloudbuild.builds.editor, logging.viewer, monitoring.viewer, run.developer, **secretmanager.secretAccessor**, secretmanager.viewer, serviceusage.serviceUsageConsumer |
| `ai2d@` on the SA | `roles/iam.serviceAccountUser` — already bound |
| Owner / Editor | Owner = swaroop@ only. **No editor.** Clean. |
| Artifact Registry | `lqabr` repo exists, us-central1, images present |
| Secrets | 13 `lqabr-*`, all with values. **Zero per-secret IAM bindings** — all access via the project-wide grants above |
| VPC / NAT | none (compute API off) |

### Live Cloud Run services

| Service | Public? | maxScale | ingress | VPC egress | Last deployed |
| --- | --- | --- | --- | --- | --- |
| `lqabr-dev-gtwy` | **yes (allUsers)** | **20** | all (default) | none | 2026-08-05 |
| `lqabr-dev-gtwy-pub` | no | 20 | all | none | 2026-08-04 |
| `lqabr-dev-email-agent` | no | (unset) | all | none | 2026-08-03 |
| `lqabr-dev-txtv` | no | 20 | all | none | 2026-08-04 |
| `lqabr-dev-ldpf` | no | 20 | all | none | 2026-08-04 |

Target set is `gtwy, mcp, summary, research, email, txtv` → **missing `mcp`, `summary`,
`research`; extra `gtwy-pub`, `ldpf`.**

### Issues (open)

- **P0-I8 — LIVE RISK: gateway `maxScale=20` with an in-memory dedupe store. VERIFIED.**
  `agents/gateway/src/router.py:400` holds dedupe as a per-process `OrderedDict`.
  `infra/gcp/07_deploy_gateway.sh:46` pins max=1 precisely because "raising this without a
  shared store WILL allow duplicate agent dispatches on HubSpot retries." The live service
  runs **20**. This is a present risk of duplicate emails/calls to real leads, not a future
  one. Status: **TODO — fix on the live service, and pin in every future deploy.**
- **P0-I9 — `--vpc-egress=all-traffic` on all six would break the private mesh.**
  Under all-traffic + NAT, a gateway->agent call to `*.run.app` exits via NAT to a public IP
  and re-enters the callee's PUBLIC front end, which `ingress=internal` rejects. Scope
  all-traffic to SaaS-facing hops only, or add a private DNS zone for `run.app` ->
  199.36.153.4/30. Status: **TODO.**
- **P0-I10 — txtv MCP env var mismatch. VERIFIED.** Doc emits `LQABR_TXTV_MCP_BASE_URL`;
  `agents/text_voice/src/mcp_client.py:23` reads `LQABR_MCP_BASE_URL`, defaulting to
  `http://localhost:8080/mcp`. txtv would point at itself and fail silently. Status: **TODO.**
- **P0-I5 — ID-token auth is NOT implemented. VERIFIED.** `agents/gateway/lib/soloai/protocols/a2a.py:220`
  builds headers with correlation IDs only — no `Authorization`. Call sites needing the fix:
  `a2a.py`, `agents/gateway/src/call_report.py`, `agents/research/packages/research_core/mcp/client.py`,
  `agents/summary/packages/summary_core/mcp/client.py`, `agents/text_voice/src/mcp_client.py`.
  Status: **TODO — P8, before deploy.**
- **P0-I7 — Two components bypass the MCP and hold HubSpot tokens directly. VERIFIED.**
  (a) `agents/email/src/outreach.py:57` imports `mcp.hubspot.server` in-process.
  (b) `agents/gateway/src/audience.py:51` holds a HubSpot token for audience resolution.
  So the doc's "MCP is the only door to HubSpot" is false for **two** services, not one.
  Under D2 (one SA) this is moot for IAM, but the doc's §1/§5 claims need correcting.
  Status: **TODO — doc correction.**
- **P0-I18 — Deployer group can read secret VALUES.** `ai2d@` holds project-wide
  `roles/secretmanager.secretAccessor`. `SETUP_CLOUDRUN_IAM.md` §2.6 explicitly states
  `aidcld@` "cannot ... touch a secret's *value*". The live grant contradicts the intended
  model. `secretmanager.viewer` (metadata only) is the role §2.6 actually calls for.
  Status: **TODO — recommend removing the accessor grant.**
- **P0-I19 — Cloud stack is ~3 weeks stale.** Last deploys 2026-08-03..05; the local MVP
  snapshot is 2026-08-23 and added summary, research and the MCP container, none of which
  have ever been deployed. This run is a **rebuild**, not an increment. Status: **noted.**
- **P0-I12 — No scheduler identity.** CLAUDE.md §3 says Cloud Scheduler drives dispatch;
  `cloudscheduler` API is on, but no scheduler SA or invoker row exists. Status: **TODO.**
- **P0-I15 — `aiplatform.user` is granted but models are Anthropic/AI-Studio** per CLAUDE.md §3.
  Status: **TODO — candidate for removal.**
- **P0-I17 — Five parallel deploy paths.** `infra/gcp/00-07`, `infra/dev/deploy/deploy.sh`
  (+`prod/`, third env name `LQABR_MCP_URL`), `agents/summary/infra/03_deploy_run.sh`,
  `infra/docker-compose.yml`, and this doc. Service names disagree
  (`lqabr-agent-gateway` vs `lqabr-dev-gtwy`). Status: **TODO — pick one, deprecate rest.**
- **P0-I6 — Build assets cover only 3 of 6 components.** `infra/docker-compose.yml` builds
  `email-agent`, `gtwy`, `ldpf` only. Need entries for `mcp`, `summary`, `research`, `txtv`.
  Own Dockerfiles exist for `mcp/`, `gateway`, `summary`, `text_voice`; `research`/`email`
  use the shared `infra/gcp/cloud-run/Dockerfile` (`SERVICE_KIND=service`). Status: **TODO.**
- **P0-I4 — 2 Vapi secrets do not exist** in any project and need values from the user.
  Also: keep the existing `lqabr-hubspot-webhook-secret` name; do NOT create a second
  `lqabr-hubspot-app-secret`. Status: **BLOCKED on user-supplied values.**

### Resolved / dropped by the decisions

- P0-I1, P0-I11 (shared-project risk) — **RESOLVED by D1.**
- P0-I2 (fate of old stack) — **RESOLVED by D1**: it IS the target; services get rebuilt.
- P0-I3 (missing AR repo) — **RESOLVED by D1**: `lqabr` already exists.
- P0-I13, P0-I14, P0-I16 — **moot or deferred** under D1/D2.

---

## P1 — Enable `compute.googleapis.com` — **DONE (with one open sub-task)**

Approved by user 2026-08-25. Deviations from doc, both deliberate:
- **Dropped `vpcaccess.googleapis.com`** — that API serves Serverless VPC Access *connectors*;
  this build uses **Direct VPC egress**, which does not need it. Avoids ~$25-70/mo of
  always-on connector VMs. Confirmed post-hoc: the API is still disabled and
  `vpc-access connectors list` errors out, so nothing depends on it.

```bash
gcloud services enable compute.googleapis.com --project=ldqfingsrv-dev
```

Result: `Operation "operations/acf.p2-432617526728-e8105857-82b4-4e65-a603-1641d682f261"
finished successfully.` Verified: `compute.googleapis.com` now listed as enabled.

### Sub-tasks

- **P1-S1 — Default VPC auto-created, as predicted. RESOLVED 2026-08-25.**
  `constraints/compute.skipDefaultNetworkCreation` is NOT enforced on org 621891143198, so
  enabling the API created an AUTO-mode network `default` with four firewall rules:

  | Rule | Source | Allows |
  | --- | --- | --- |
  | `default-allow-ssh` | **0.0.0.0/0** | tcp:22 |
  | `default-allow-rdp` | **0.0.0.0/0** | tcp:3389 |
  | `default-allow-icmp` | 0.0.0.0/0 | icmp |
  | `default-allow-internal` | 10.128.0.0/9 | tcp/udp:0-65535, icmp |

  Dependency check — **all clear, nothing uses it**: 0 compute instances, 0 forwarding
  rules, 0 reserved addresses, 0 routers; `cloudfunctions` API disabled; `vpcaccess` API
  disabled (so no connectors).

  Removal was first attempted by Claude and **blocked by the Claude Code auto-mode
  classifier** (a local harness guardrail on destructive commands, NOT a GCP permission
  failure). From this point the user executes all commands directly and Claude analyses
  output — see the working-mode change logged below.

  **Executed by user, succeeded:** all four rules deleted, then the network. Confirmed by
  `Deleted [...]` for each of the five resources. Note the network delete fails with
  "already being used by ... default-allow-rdp" until every rule is gone — the four rules
  must be deleted first, and batching all four names into one `delete` call is more reliable
  than a shell loop.

  Commands run:
  ```bash
  for R in default-allow-icmp default-allow-internal default-allow-rdp default-allow-ssh; do
    gcloud compute firewall-rules delete $R --project=ldqfingsrv-dev --quiet
  done
  gcloud compute networks delete default --project=ldqfingsrv-dev --quiet
  ```
  Reversible: `gcloud compute networks create default --subnet-mode=auto` restores the
  network and its default rules.

  Note: leaving `default` in place does NOT block P6 — `lqabr-vpc` is a separate custom
  network. The exposure is latent (no instances to reach), but the two 0.0.0.0/0 rules
  become live the moment anyone creates a VM in this project.

- **P1-S2 — `compute.skipDefaultNetworkCreation` is unenforced org-wide.** Every future
  project in org 621891143198 will repeat this. Worth an org-policy change outside this run.
  Status: **TODO — out of scope, flagged.**

- **P1-S3 — `iam.allowedPolicyMemberDomains` is `allValues: ALLOW`.** Checked because it
  would have blocked the gateway's `allUsers` binding. It does not. No action.

---

## Revised phase list

| # | Phase | Status |
| --- | --- | --- |
| P1 | Enable `compute` API (`vpcaccess` dropped) | **DONE** |
| P2 | AR repo — exists; add image cleanup policy | TODO (repo itself SKIPPED) |
| P3 | Runtime SA — `lqabr-agent-dev` exists | **SKIPPED** |
| P4 | SA project roles — already present; review `aiplatform.user` | TODO (review only) |
| P5 | Create 2 Vapi secrets; remove `secretAccessor` from `ai2d@` | BLOCKED (needs values) |
| P6 | VPC + subnet (+private-ip-google-access) + router + static IP + NAT | TODO |
| P7 | Deployer group — roles + `actAs` already bound | **SKIPPED** (except P5's removal) |
| P8 | CODE: ID-token attach (5 sites) + txtv env var fix + tests | TODO |
| P9 | Build + push 6 images; deploy with corrected flags | TODO |
| P10 | Retire `gtwy-pub`/`ldpf`; repoint HubSpot webhook; verify | TODO |

**Corrections baked into P9:** gateway `--max-instances=1 --min-instances=1`;
egress scoped per P0-I9; `LQABR_MCP_BASE_URL` (not `LQABR_TXTV_MCP_BASE_URL`) for txtv.

---

## Run log

| When | Phase | Event |
| --- | --- | --- |
| 2026-08-25 | P0 | Grounded `leadgen-snbox-11b7c`; 7 issues logged. |
| 2026-08-25 | P0 | architect-critic REVISE (51/100); cost view ~$4/mo floor. 10 more issues. |
| 2026-08-25 | P0 | Decisions D1-D3 taken. Re-grounded on `ldqfingsrv-dev`. Plan rewritten. |
| 2026-08-25 | P1 | `compute.googleapis.com` enabled. `default` VPC auto-created; deletion blocked by harness guard (P1-S1). |
| 2026-08-25 | P1 | **Working mode changed:** user executes all commands, Claude analyses output and directs. No scripts/tests/validations created unless requested. |
| 2026-08-25 | P1 | P1-S1 RESOLVED by user — 4 default firewall rules + `default` network deleted. Project now has zero VPCs. |
