# LQABR Cloud Run — Runbook

**Project:** `ldqfingsrv-dev` · **Region:** `us-central1` · **Runtime SA:** `lqabr-agent-dev` (single)
**Operator:** `swaroop@aidefinitive.com` (Owner) · **Started:** 2026-08-25

Derived from `docs/SETUP_CLOUDRUN_IAM.md`, adapted by the decisions below.
Live execution status is tracked separately in `docs/runs/CLOUDRUN_SETUP_RUN.md`.

Each phase has three sections:
1. **End results** — what exists once the phase is complete.
2. **Commands** — the *correct* command sequence, with any prerequisite recorded as a
   numbered dependency. Commands that failed during execution are not reproduced; only the
   working form, plus whatever extra step was needed to make it work.
3. **Validation** — how to prove the phase actually landed.

### Decisions that shape this runbook

| # | Decision | Effect |
| --- | --- | --- |
| D1 | Deploy into **`ldqfingsrv-dev`**, not the doc's `leadgen-snbox-11b7c` | AR repo, runtime SA and 5 secrets already exist. Avoids the shared-project blast-radius problem. |
| D2 | **One runtime SA** (`lqabr-agent-dev`), not the doc's six | Accepts current posture. The doc's §2.4 per-secret table collapses to one project-wide accessor. |
| D3 | **6 secrets**, not 13 | Twilio ×2, Zoom ×4, ZoomInfo ×2 stay dormant — no ingestion or scheduling agent exists in this snapshot. |
| D4 | **No `lqabr-vapi-webhook-secret`** | txtv polls Vapi for call status; it does not receive webhooks. `vapi.report.enabled` stays `false`, so the gateway relay — the only consumer of that secret — is never used. |
| D5 | **No `vpcaccess.googleapis.com`** | Direct VPC egress is used instead of Serverless VPC Access connectors. Avoids ~$25–70/mo of always-on connector VMs. |
| D6 | **`ldpf` (Lead Profile agent) is retained** | The target set is **7** components, not the doc's 6: `gtwy, mcp, summary, research, email, txtv, ldpf`. |
| D7 | **MCP image is promoted from Docker Hub**, not built from `mcp/Dockerfile` | `tne736/lqabr-mcp-server:latest` is the current build. Provenance trade-off accepted for now — see the open item in P2. |
| D8 | **All container images run as a single non-root user `agent` (uid 1001)** | Uniform, not per-component. A per-component uid (`e_agent`, `re_agent`, …) was considered and rejected — see P2c for the reasoning. |
| D9 | **Deployer group keeps `secretmanager.secretAccessor` in dev**; the doc is corrected rather than the permission | `setup_env.sh` depends on it for local onboarding. Recorded as a known deviation from §2.6, with a group-split fix scheduled for production — see P4. |

**Source-doc corrections applied 2026-08-26.** `docs/SETUP_CLOUDRUN_IAM.md` was amended in
three places so it no longer asserts controls that do not exist: a header note marking it
superseded in part (wrong project, six SAs vs one), the §2.6 "cannot touch a secret's value"
claim, and the §5 threat-table row "Deployer key abuse". That document remains the design
rationale; **this runbook is authoritative for what is actually built.**

---

## P1 — Enable required Google Cloud APIs

**Status: DONE (2026-08-25)**

### 1. End results

- 10 APIs enabled on `ldqfingsrv-dev`:

  | API | Used for |
  | --- | --- |
  | `run.googleapis.com` | Cloud Run services |
  | `cloudbuild.googleapis.com` | building container images |
  | `artifactregistry.googleapis.com` | the `lqabr` image repository |
  | `secretmanager.googleapis.com` | the `lqabr-*` secrets |
  | `compute.googleapis.com` | VPC, subnet, router, Cloud NAT |
  | `logging.googleapis.com` | Cloud Logging |
  | `monitoring.googleapis.com` | Cloud Monitoring |
  | `aiplatform.googleapis.com` | Vertex AI |
  | `pubsub.googleapis.com` | Pub/Sub |
  | `cloudscheduler.googleapis.com` | Cloud Scheduler |

- **Only `compute.googleapis.com` was enabled by this run.** The other nine were already
  enabled from the August 2026 work.
- `vpcaccess.googleapis.com` is deliberately **NOT** enabled (see D5).
- No `default` VPC network exists in the project.

### 2. Commands

**Step 1 — enable the Compute Engine API.**

```bash
gcloud services enable compute.googleapis.com --project=ldqfingsrv-dev
```

**Step 2 — remove the auto-created `default` network.**

> **Dependency — why this step exists.** The org policy
> `constraints/compute.skipDefaultNetworkCreation` is **not enforced** on org
> `621891143198`. Enabling the Compute API therefore auto-creates an AUTO-mode VPC named
> `default`, with subnets in every region and four firewall rules — including
> `default-allow-ssh` (tcp:22) and `default-allow-rdp` (tcp:3389) open to `0.0.0.0/0`.
> This is latent exposure the moment anyone creates a VM in the project.

> **Dependency — confirm nothing uses the network before deleting.** All six must be empty:
> ```bash
> gcloud compute instances list         --project=ldqfingsrv-dev
> gcloud compute forwarding-rules list  --project=ldqfingsrv-dev
> gcloud compute addresses list         --project=ldqfingsrv-dev
> gcloud compute routers list           --project=ldqfingsrv-dev
> gcloud functions list                 --project=ldqfingsrv-dev
> gcloud compute networks vpc-access connectors list --region=us-central1 --project=ldqfingsrv-dev
> ```
> Expect `Listed 0 items.` for the first four. The last two returning
> "API has not been used in project" is also a pass — it proves no Functions and no VPC
> connectors exist.

> **Dependency — firewall rules MUST be deleted before the network.** `networks delete`
> fails with `The network resource '...' is already being used by
> 'projects/.../firewalls/default-allow-rdp'` while any rule still references it. Delete
> all four names in **one** `delete` call rather than a shell loop — a loop can stop
> partway and leave the network undeletable with no obvious cause.

```bash
gcloud compute firewall-rules delete \
  default-allow-icmp default-allow-internal default-allow-rdp default-allow-ssh \
  --project=ldqfingsrv-dev --quiet

gcloud compute networks delete default --project=ldqfingsrv-dev --quiet
```

> **Note.** Deletes are asynchronous. If `networks delete` still reports the network in use
> immediately after the rules report deleted, wait ~15s and retry once before treating it
> as a real failure.

### 3. Validation

**Confirm all 10 APIs are enabled:**

```bash
gcloud services list --enabled --project=ldqfingsrv-dev --format='value(config.name)' \
  | grep -E '^(run|cloudbuild|artifactregistry|secretmanager|compute|aiplatform|logging|monitoring|pubsub|cloudscheduler)\.' \
  | sort
```

Expect exactly 10 lines.

**Confirm `vpcaccess` is NOT enabled:**

```bash
gcloud services list --enabled --project=ldqfingsrv-dev --format='value(config.name)' | grep vpcaccess
```

Expect no output.

**Confirm the project has no VPC and no firewall rules:**

```bash
gcloud compute networks list       --project=ldqfingsrv-dev
gcloud compute firewall-rules list --project=ldqfingsrv-dev
```

Expect `Listed 0 items.` from both.

---

## P2 — Artifact Registry

**Status: DONE (2026-08-26). Cleanup + MCP promotion both complete.**

### 1. End results

- Docker repository `lqabr` in `us-central1` (**pre-existing**, created 2026-07-21).
  Nothing to create.
- Three obsolete image packages removed, so every remaining family maps to a real service:

  | Package | Action | Reason |
  | --- | --- | --- |
  | `permission-test` | **deleted** | junk left over from a permissions experiment |
  | `lqabr-dev-email-webhook` | **deleted** | retired — `infra/docker-compose.yml` records the image as removed; Mailgun now pushes to the same service the gateway reaches |
  | `lqabr-dev-text-voice-webhook` | **deleted** | retired in Rev 5 — see CLAUDE.md §10 |
  | `lqabr-dev-gtwy` | kept | live service |
  | `lqabr-dev-email-agent` | kept | live service |
  | `lqabr-dev-txtv` | kept | live service |
  | `lqabr-dev-ldpf` | kept | live service |

- **State before this phase:** 35 images across 7 packages, 829.334 MB, 30 untagged,
  no cleanup policy, vulnerability scanning disabled.

**Cost note.** This phase is hygiene, not saving. 829 MB against a 0.5 GB free tier is
roughly **$0.03/mo**. The real benefit is that two retired families and 30 untagged blobs
made it genuinely hard to tell which image a service runs — a problem the repo already
acknowledges, since `infra/docker-compose.yml` warns that `05_deploy_agents.sh` uses a
naming scheme that "does NOT match the live services."

**Deferred — cleanup policy (Step 3).** `gcloud artifacts repositories set-cleanup-policies`
requires a JSON policy file. Deferred until after P9, when six new images make the real
churn pattern visible and a keep-last-N rule can be set from evidence rather than guesswork.

### 2. Commands

> **Dependency — confirm no live service references an image before deleting it.**
> Artifact Registry deletion is **irreversible**; there is no undelete. Deleting an image a
> service points at does not stop the running revision, but it does prevent that service
> from ever starting a new one.
> ```bash
> gcloud run services list --project=ldqfingsrv-dev \
>   --format='value(metadata.name,spec.template.spec.containers[0].image)'
> ```
> The five live services resolve only to `gtwy`, `email-agent`, `txtv` and `ldpf` — neither
> retired webhook family appears, so all three deletions below are safe.

```bash
gcloud artifacts docker images delete \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/permission-test \
  --project=ldqfingsrv-dev --delete-tags --quiet

gcloud artifacts docker images delete \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-email-webhook \
  --project=ldqfingsrv-dev --delete-tags --quiet

gcloud artifacts docker images delete \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-text-voice-webhook \
  --project=ldqfingsrv-dev --delete-tags --quiet
```

> **Note.** `--delete-tags` is required when a package still has tagged versions; without it
> the delete is refused. Each call returns `Delete request issued.` and then blocks on a
> long-running operation — all three completed successfully on 2026-08-25.

### 2b. Image inventory — bringing AR in line with the target set

AR was not *broken*, it was **incomplete and stale**. Nothing needed fixing; three packages
are missing and three are three weeks old.

| Component | In AR | Age | Action |
| --- | --- | --- | --- |
| `lqabr-dev-gtwy` | yes | 2026-08-03 | rebuild at P9 |
| `lqabr-dev-email-agent` | yes | 2026-08-03 | rebuild at P9 |
| `lqabr-dev-txtv` | yes | 2026-08-04 | rebuild at P9 |
| `lqabr-dev-ldpf` | yes | 2026-08-04 | **retained** (D6); rebuild at P9 |
| `lqabr-dev-mcp` | **yes — pushed 2026-08-26** | today | promoted from Docker Hub (D7) |
| `lqabr-dev-summary` | **no** | — | build at P9 |
| `lqabr-dev-research` | **no** | — | build at P9 |

**Why image builds belong in P9, not here.** An image built before P8 (the ID-token change)
cannot authenticate to a private MCP, so building now guarantees rebuilding later. Only the
MCP image is promoted at this stage, because it is taken as-is rather than built from source.

#### Promoting the MCP image

> **Dependency — confirm the image is `linux/amd64` before promoting.** Cloud Run runs
> containers on x86-64 only; there is no ARM runtime. A third-party Docker Hub image built
> on Apple Silicon without `buildx` multi-arch is arm64-only, runs fine locally under
> emulation, and then fails on Cloud Run with "container failed to start and listen on port"
> — an error that never mentions architecture.
> ```bash
> docker manifest inspect tne736/lqabr-mcp-server:latest | grep -E '"architecture"|"os"'
> ```
> **Verified 2026-08-26:** returned `"architecture": "amd64"` / `"os": "linux"`. The
> additional `"unknown"/"unknown"` entry is the buildx attestation manifest
> (provenance/SBOM) present on every buildx image — not a real platform, safe to ignore.

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

docker pull tne736/lqabr-mcp-server:latest

docker tag tne736/lqabr-mcp-server:latest \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-mcp:0.1.0
docker tag tne736/lqabr-mcp-server:latest \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-mcp:latest

docker push us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-mcp:0.1.0
docker push us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-mcp:latest
```

> **Why both tags.** Cloud Run does not auto-pull `:latest`. Deploy against the immutable
> `0.1.0` tag so a revision always resolves to a known image; `:latest` is a convenience
> pointer only.

> **Dependency — Docker auth fails in WSL.** `gcloud auth configure-docker` registers a
> credential helper, but if `docker-credential-gcloud` is not on PATH (common when Docker
> Desktop runs from the Windows side and cannot see WSL's gcloud), the push fails with
> `error from registry: Unauthenticated request ... artifactregistry.repositories.uploadArtifacts`.
> The helper is registered and reports success, so the failure looks like an IAM problem
> when it is not. Bypass it:
> ```bash
> gcloud auth print-access-token | \
>   docker login -u oauth2accesstoken --password-stdin https://us-central1-docker.pkg.dev
> ```

> **Dependency — Cloud Run cannot pull from Docker Hub.** Images must live in Artifact
> Registry or Container Registry. Deploying `tne736/lqabr-mcp-server:latest` directly fails;
> the tag+push above is what makes it reachable. (Alternative not taken: an AR **remote
> repository** proxying Docker Hub, which would auto-track upstream.)

**Executed 2026-08-26 — pushed successfully. Digest pin:**
```
sha256:1d5c84c7651808a769e78031e40c03e1c8e3dc519b9829d7cfad92de4603aeef
```
Both `0.1.0` and `latest` resolve to this digest. **This is the provenance record** the
open item below asks for — `:latest` on Docker Hub is mutable, so this digest is the only
durable statement of what was actually promoted.

**Image contract — settled by inspecting the image, not the repo.**

```bash
docker image inspect tne736/lqabr-mcp-server:latest --format '{{json .Config.Env}}'
```
```
MCP_TRANSPORT=streamable-http   MCP_HOST=0.0.0.0   PORT=8080
MCP_PATH=/mcp                   HUBSPOT_AUTH_MODE=private_app
```

> **CORRECTION.** The repo's `mcp/` directory did **not** build this image, so its
> `LQABR_`-prefixed variables (`LQABR_MCP_PATH`, `LQABR_SECRETS_SOURCE`,
> `LQABR_HUBSPOT_ACCESS_TOKEN`) are **not** this image's contract. The image uses
> **unprefixed** names, and `HUBSPOT_AUTH_MODE=private_app` is baked in — so the token
> variable is `HUBSPOT_PRIVATE_APP_TOKEN`. `SETUP_CLOUDRUN_IAM.md` §3's
> `LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN` is also wrong, but closer.
>
> Note a Secret Manager lookup **cannot** answer this: Secret Manager stores a secret's
> *name* and *value*, never the environment variable a consumer expects. The image is the
> only authority.

> **OPEN — MCP provenance.** `tne736/lqabr-mcp-server:latest` is a public Docker Hub tag
> that is (a) mutable, so "the current build" is unreproducible later, and (b) not built
> from this repo, while `mcp/Dockerfile` exists and is unused. The MCP holds the **only**
> HubSpot credential in the system. Capture the digest at promotion time
> (`docker image inspect ... --format '{{index .RepoDigests 0}}'`) and decide before
> production whether to switch to `docker build --platform linux/amd64 -f mcp/Dockerfile .`
> Open question for the team: is the Docker Hub image built from this repo?

> **Also unresolved — two competing local MCP images.** `mcp/mcp.config` runs
> `tne736/lqabr-mcp-server:latest`, but `agents/summary/infra/docker-compose.yml` expects
> `lqabr-hubspot-mcp:local`. Both exist in the local Docker daemon. They must converge on
> one image before P9.

### 3. Validation

**Confirm the MCP image landed — NOT YET CONFIRMED:**

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr \
  --include-tags --format='table(package,tags,updateTime.date("%Y-%m-%d"))' | grep mcp
```

Expect one `lqabr-dev-mcp` row tagged `0.1.0,latest`. A run on 2026-08-26 returned **no
row**, meaning the push had not yet been executed at that point.

**Confirm the surviving packages — expect exactly 4:**

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr \
  --format='value(package)' | sort | uniq -c
```

Expect `lqabr-dev-gtwy`, `lqabr-dev-email-agent`, `lqabr-dev-txtv`, `lqabr-dev-ldpf` —
plus `lqabr-dev-mcp` once promoted. The three deleted names must be absent.

**Confirm repository size dropped from 829.334 MB:**

```bash
gcloud artifacts repositories describe lqabr \
  --location=us-central1 --project=ldqfingsrv-dev \
  --format='value(name,sizeBytes)'
```

**Confirm no live service lost its image — the check that actually matters:**

```bash
gcloud run services list --project=ldqfingsrv-dev \
  --format='table(metadata.name,spec.template.spec.containers[0].image)'
```

Every row must still resolve to `gtwy`, `email-agent`, `txtv` or `ldpf`. A row showing a
deleted name means that service can no longer start a new revision and its image must be
rebuilt.

**Confirm remaining untagged count (was 30) — informs the deferred cleanup policy:**

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr \
  --include-tags --format='value(tags)' | grep -c '^$'
```

> **Outstanding.** The four validation commands above have **not yet been run**. P2's
> deletions are confirmed by their own operation output, but the post-state has not been
> independently verified.

---

## P2c — Container runtime user (uid standardisation)

**Status: DONE (2026-08-26) — source change only, not yet built or pushed**

### 1. End results

Every LQABR image runs as the **same non-root user: `agent`, uid 1001**.

| Image | Before | After |
| --- | --- | --- |
| `infra/gcp/cloud-run/Dockerfile` (email, research, ldpf) | `nobody` (65534) | **`agent`** (1001) |
| `agents/text_voice/Dockerfile` | `nobody` (65534) | **`agent`** (1001) |
| `agents/gateway/Dockerfile` | **root** — no `USER` directive at all | **`agent`** (1001) |
| `agents/summary/Dockerfile` | `agent` (1001) | unchanged — this was the model |
| `mcp/Dockerfile` | **root** — no `USER` directive | **unchanged, deferred** |

Why it mattered: the gateway ran as **root** inside its container, and the rest were split
between two different uids, so the same `chown` recipe did not work across images.

**MCP is deliberately deferred.** It is the only component holding a HubSpot credential, so
it warrants its own review before its runtime user changes.

### 2. Commands

Source edits only — no gcloud, no build.

**`infra/gcp/cloud-run/Dockerfile`** — added after `WORKDIR /app`:
```dockerfile
RUN useradd --create-home --uid 1001 agent
```
then `chown -R nobody /var/lib/lqabr` → `chown -R agent:agent /var/lib/lqabr`,
`chown -R 65534:65534 "agents/${AGENT_DIR}"` → `chown -R agent:agent "agents/${AGENT_DIR}"`,
and `USER nobody` → `USER agent`.

**`agents/text_voice/Dockerfile`** — same `useradd`, plus a `chown -R agent:agent /app`
(it previously had none, relying on `nobody` needing only read access), and
`USER nobody` → `USER agent`.

**`agents/gateway/Dockerfile`** — same `useradd`, plus `chown -R agent:agent /app` and a new
`USER agent` before `EXPOSE 8080`.

> **Dependency — the `agentgateway` binary.** The gateway installs it at build time via
> `curl -sL https://agentgateway.dev/install | bash`, running as root. If the installer puts
> it on `PATH` (`/usr/local/bin`), `agent` finds it. If it puts it under root's `$HOME`,
> `agent` will not, and `agents/gateway/docker-entrypoint.sh:28` logs *"agentgateway binary
> not present (D-02)"* and falls back to direct dispatch via `LQABR_*_AGENT_URL`. That
> fallback is existing designed behaviour, **but it is a silent degradation** — confirm on
> the first build rather than assuming.

### Rejected: per-component uids

Considered `e_agent` / `re_agent` etc. per component, on the reasoning that a distinct uid
makes a process signature more uniquely attributable. **Rejected**, because:

- Each component runs in its own container and kernel namespace. `ps -ef` can never show
  another component's processes, so inside the email container every process is already
  email's — the uid adds no information.
- Cloud Run already stamps `resource.labels.service_name` on every log line, which is more
  precise than a uid and needs no maintenance.
- It would add fine granularity to the layer where it changes nothing, while D2 keeps a
  **single service account** — the layer that actually gates secrets, audit identity and
  blast radius. If per-component separation is wanted, per-component *service accounts* are
  the lever, not uids.
- The shared Dockerfile is parametrised by `AGENT_DIR`, so per-component users mean
  parametrising `RUN_USER`/`RUN_UID` too. A mismatched `--build-arg` then fails at
  **runtime**, not build time.

Worth revisiting only if these components ever share a kernel (GKE nodes, or multiple
processes per container).

### 3. Validation

Not yet run — requires a build.

```bash
docker build -f agents/gateway/Dockerfile -t lqabr-gtwy:test .
docker run --rm --entrypoint sh lqabr-gtwy:test \
  -c 'id; command -v agentgateway || echo "NOT ON PATH for agent"'
```

Expect `uid=1001(agent) gid=1001(agent)` and a path for the binary. `NOT ON PATH` means the
install step must be changed to place it in `/usr/local/bin` explicitly.

**Static check across all images:**
```bash
for f in infra/gcp/cloud-run/Dockerfile agents/gateway/Dockerfile \
         agents/summary/Dockerfile agents/text_voice/Dockerfile mcp/Dockerfile; do
  printf "%-40s USER=%s\n" "$f" "$(grep -E '^USER' $f | tr '\n' ' ')"
done
```
Expect `USER agent` for the first four and empty for `mcp/Dockerfile` (deferred).

---

## P3 — Service accounts

**Status: runtime SA pre-existing (verified). Build SA CREATED 2026-08-26.**

### 1. End results

Three service accounts exist in `ldqfingsrv-dev`:

| Service account | Purpose | Origin |
| --- | --- | --- |
| `lqabr-agent-dev@…` | **runtime** — every Cloud Run service runs as this (D2) | pre-existing |
| `lqabr-build@…` | **Cloud Build** — builds and pushes images | **created this run** |
| `432617526728-compute@…` | GCP default compute SA — **not used by LQABR** | GCP default |

> **Why a build SA was needed — and why it did not surface until P9.**
> MCP was promoted by pulling from Docker Hub and `docker push`-ing straight to
> Artifact Registry as **`swaroop@`**, so no build identity was ever involved. The first
> Cloud Build submission failed:
>
> ```
> ERROR: (gcloud.builds.submit) INVALID_ARGUMENT: could not resolve source:
> 432617526728-compute@developer.gserviceaccount.com does not have storage.objects.get
> access to the ... _cloudbuild ... object
> ```
>
> Cloud Build now defaults to the **compute** SA rather than the legacy
> `<project-number>@cloudbuild.gserviceaccount.com`, and the compute SA in this project
> holds **zero** roles. Pointing the build at the legacy SA — which *does* hold
> `roles/cloudbuild.builds.builder` — was rejected outright:
>
> ```
> invalid value for `build.service_account`: provide a user-managed service account
> ```
>
> Hence a user-managed SA. Note this is a **build** identity, distinct from the runtime SA;
> push and deploy in this run were both performed by `swaroop@`, not by any service account.

**Roles granted to `lqabr-build` — two of three are resource-scoped:**

| Role | Scope | Why |
| --- | --- | --- |
| `roles/logging.logWriter` | project | build logs; Cloud Logging has no narrower scope |
| `roles/artifactregistry.writer` | **repo `lqabr` only** | push images |
| `roles/storage.objectViewer` | **bucket `ldqfingsrv-dev_cloudbuild` only** | read the uploaded source tarball |

`roles/cloudbuild.builds.builder` was deliberately **not** used — it is a broad project-level
role, and granting it to the default compute SA (the alternative fix) would have given build
rights to the identity other Google services fall back to.

### 2. Commands

The runtime SA required nothing — it pre-dates this run. The build SA:

```bash
gcloud iam service-accounts create lqabr-build \
  --display-name="LQABR Cloud Build" --project=ldqfingsrv-dev

gcloud projects add-iam-policy-binding ldqfingsrv-dev \
  --member="serviceAccount:lqabr-build@ldqfingsrv-dev.iam.gserviceaccount.com" \
  --role=roles/logging.logWriter

gcloud artifacts repositories add-iam-policy-binding lqabr \
  --location=us-central1 --project=ldqfingsrv-dev \
  --member="serviceAccount:lqabr-build@ldqfingsrv-dev.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.writer

gcloud storage buckets add-iam-policy-binding gs://ldqfingsrv-dev_cloudbuild \
  --member="serviceAccount:lqabr-build@ldqfingsrv-dev.iam.gserviceaccount.com" \
  --role=roles/storage.objectViewer
```

> **Dependency — every build must now pass `--service-account`.** Omitting it falls back to
> the role-less compute SA and reproduces the original error. Wired into
> `agents/<agent>/infra/config.sh` as `BUILD_SA` and passed by
> `agents/<agent>/infra/02_build_push.sh`, so it is not a flag anyone has to remember.

### 3. Validation

```bash
gcloud iam service-accounts list --project=ldqfingsrv-dev --format='table(email,displayName,disabled)'
```

Verified 2026-08-26 — three accounts, `lqabr-agent-dev` ("LQABR agent runtime") and
`lqabr-build` ("LQABR Cloud Build") both enabled.

```bash
gcloud artifacts repositories get-iam-policy lqabr --location=us-central1 --project=ldqfingsrv-dev
gcloud storage buckets get-iam-policy gs://ldqfingsrv-dev_cloudbuild
```

Verified — `lqabr-build` holds `artifactregistry.writer` on the repo and
`storage.objectViewer` on the bucket.

> **Watch item — the default compute SA.** It holds **no** project roles, which is why the
> build failed rather than silently succeeding with excess privilege. Leave it that way:
> Cloud Run and Cloud Build both fall back to it when an identity is not specified, so every
> deploy must pass `--service-account` and every build `--service-account`.

---

## P4 — Project-level IAM

**Status: VERIFIED (2026-08-26) — no changes applied. One accepted deviation, one deferred cleanup.**

### 1. End results

IAM is unchanged from the August 2026 state. It was reviewed, not modified.

**Runtime service account — `lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com`**

| Role | Verdict |
| --- | --- |
| `roles/logging.logWriter` | required |
| `roles/monitoring.metricWriter` | required |
| `roles/run.invoker` | required — gateway→agents, agents→MCP |
| `roles/secretmanager.secretAccessor` | required (project-wide, per D2) |
| `roles/aiplatform.user` | **unused** — models are Anthropic (CLAUDE.md §3) |
| `roles/pubsub.publisher` | **unused** — no ingestion/orchestrator agent exists |
| `roles/pubsub.subscriber` | **unused** — same |

**Deployer group — `group:ai2d@aidefinitive.com`**

Six roles match `SETUP_CLOUDRUN_IAM.md` §2.6 exactly: `run.developer`,
`cloudbuild.builds.editor`, `artifactregistry.writer`, `secretmanager.viewer`,
`logging.viewer`, `monitoring.viewer`. Two are additional:

| Extra role | Verdict |
| --- | --- |
| `roles/serviceusage.serviceUsageConsumer` | benign; commonly required to consume project APIs |
| `roles/secretmanager.secretAccessor` | **known deviation — see below** |

**`actAs` link:** `roles/iam.serviceAccountUser` is bound for `group:ai2d@aidefinitive.com`
on the runtime SA. This matches §2.6 and is the only intended link between deployer and
runtime.

### 2. Commands

None applied. P4 is a review phase in this run.

#### Accepted deviation — the deployer can read secret VALUES

`SETUP_CLOUDRUN_IAM.md` §2.6 asserts the deployer "cannot enable an API, mint a role, or
touch a secret's *value*." **That claim is currently false**: `ai2d@` holds project-wide
`roles/secretmanager.secretAccessor`.

**Why it is being kept for now.** `agents/text_voice/setup_env.sh:6` documents the
dependency explicitly — *"Requires only Secret Manager READ access
(roles/secretmanager.secretAccessor), which every dev already has."* That script is the
no-keys-in-chat local setup path; it pulls `lqabr-vapi-api-key` and
`lqabr-anthropic-api-key` into a git-ignored `.env`. Removing the role breaks local
onboarding for everyone in the group.

**The deploy path itself does not need it.** `--set-secrets` requires the *runtime* SA to
hold `secretAccessor`; the deployer needs only `secretmanager.viewer` to reference the
secret. This deviation exists solely to support local development.

**Why it is still wrong by enterprise RBAC standards.** Deployers should hold
`secretmanager.viewer` — enough to confirm a secret exists, is bound, and has versions —
and never `secretAccessor`. Troubleshooting almost never needs the plaintext value; it needs
to know whether the runtime SA can read it, which viewer plus logs already answers. Standing
read access makes every deployer a credential holder, so every staff change forces a
rotation. Genuine need for a value is a **break-glass** case: separate, time-bound, audited.

**Note on the real blast radius.** `ai2d@` also holds `actAs` on the runtime SA, so a member
can impersonate it and reach the same secrets regardless. The `secretAccessor` grant mainly
changes whether that access is direct or shows up as impersonation in the audit trail.

**Production fix — split the group, not the service account.** The deployer/runtime split
already exists and is correct: the deployer is a *human group*, the runtime is a *service
account*. No second service account is needed. What is needed is splitting the human group:

| Group | Holds | Purpose |
| --- | --- | --- |
| `ai2d-deploy@` | the six §2.6 roles, **no** `secretAccessor` | build, push, deploy |
| `ai2d-dev@` | `secretAccessor` only | run `setup_env.sh` for local development |

```bash
# NOT RUN — production remediation, recorded for later:
gcloud projects remove-iam-policy-binding ldqfingsrv-dev \
  --member="group:ai2d@aidefinitive.com" \
  --role=roles/secretmanager.secretAccessor
```

> **Doc correction required.** `SETUP_CLOUDRUN_IAM.md` §2.6 and its §5 threat table row
> "Deployer key abuse — `aidcld@` can deploy but not read secret values" both overstate the
> control as implemented. Correct the doc to describe the deviation rather than leave the
> claim aspirational.

#### Deferred cleanup — three unused runtime roles

`aiplatform.user`, `pubsub.publisher` and `pubsub.subscriber` are granted to the runtime SA
but nothing in the current code uses them: models route to Anthropic, and neither an
ingestion nor an orchestrator agent exists in this snapshot. Left in place deliberately —
removing them is least-privilege housekeeping with no bearing on this deploy, and they will
be needed again if those agents return.

```bash
# NOT RUN — least-privilege cleanup, only if the agents are not coming back:
# gcloud projects remove-iam-policy-binding ldqfingsrv-dev \
#   --member="serviceAccount:lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com" \
#   --role=roles/aiplatform.user
```

### 3. Validation

```bash
gcloud projects get-iam-policy ldqfingsrv-dev --flatten='bindings[].members' \
  --filter='bindings.members:lqabr-agent-dev' --format='value(bindings.role)'

gcloud projects get-iam-policy ldqfingsrv-dev --flatten='bindings[].members' \
  --filter='bindings.members:ai2d@aidefinitive.com' --format='value(bindings.role)'

gcloud iam service-accounts get-iam-policy \
  lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com \
  --project=ldqfingsrv-dev --format='value(bindings.role,bindings.members)'
```

Verified 2026-08-26: 7 runtime roles, 8 deployer roles, and `iam.serviceAccountUser` bound
for `ai2d@` on the SA — exactly as tabulated above.

---

## P5 — Secrets

**Status: 5 of 6 usable. `lqabr-hubspot-webhook-secret` EXISTS BUT IS EMPTY — blocks the gateway.**

### 1. End results

Six secrets in `ldqfingsrv-dev`, all readable by `lqabr-agent-dev` via its project-wide
`secretmanager.secretAccessor` (per D2 — no per-secret bindings):

| Secret | Consumer | State |
| --- | --- | --- |
| `lqabr-hubspot-access-token` | mcp, email, gateway | exists |
| `lqabr-hubspot-webhook-secret` | gateway (HubSpot v3 HMAC) | **EXISTS, 0 VERSIONS — unusable** |
| `lqabr-anthropic-api-key` | summary, research, txtv | exists |
| `lqabr-mailgun-api-key` | email | exists |
| `lqabr-mailgun-webhook-signing-key` | email | exists |
| `lqabr-vapi-api-key` | txtv | **created 2026-08-26, version 1** |

> ### ⛔ BLOCKER — `lqabr-hubspot-webhook-secret` has no value
> **Discovered 2026-08-26 via `lqabr-dev-gtwy` reporting `Ready: False`:**
> ```
> Secret projects/432617526728/secrets/lqabr-hubspot-webhook-secret/versions/latest was not found
> reason: SecretsAccessCheckFailed
> ```
> The gateway is serving an **old** revision (`lqabr-dev-gtwy-00002-cqh`) deployed while the
> secret still held a value; revisions `00003` and `00004` were both created and failed this
> check. This predates the current run — it was **not** caused by the P2 registry cleanup
> (the cleanup skipped every live digest, and gtwy's `793f17b4…` was explicitly protected).
>
> This is the HubSpot app's **client secret**, used for v3 webhook HMAC verification. It has
> to come from the HubSpot developer portal; it cannot be reconstructed from anything in GCP.
>
> Restore with:
> ```bash
> printf %s '<hubspot-app-client-secret>' | \
>   gcloud secrets versions add lqabr-hubspot-webhook-secret \
>     --project=ldqfingsrv-dev --data-file=-
> ```
> (`printf %s` rather than `echo` — a trailing newline breaks HMAC comparison.)
>
> **Consequence: the gateway is sequenced LAST in P9.** Deploying it before this value exists
> reproduces `SecretsAccessCheckFailed` and creates another dead revision.

Deliberately absent:
- `lqabr-vapi-webhook-secret` — not needed (D4).
- `lqabr-hubspot-app-secret` — the doc's name for a secret that already exists here as
  `lqabr-hubspot-webhook-secret`. **Keep the existing name; do not create a duplicate.**
- Twilio ×2, Zoom ×4, ZoomInfo ×2 — dormant (D3). They still exist in the project and are
  not being deleted; they are simply unused.

### 2. Commands

**Create `lqabr-vapi-api-key`.**

> **Dependency — source of the value.** The key is already present locally in
> `agents/text_voice/.env` as `LQABR_VAPI_API_KEY`. Pipe it directly so the value never
> appears in terminal history or a transcript. Do not paste it by hand.

```bash
gcloud secrets create lqabr-vapi-api-key \
  --project=ldqfingsrv-dev --replication-policy=automatic

grep '^LQABR_VAPI_API_KEY=' agents/text_voice/.env | cut -d= -f2- | tr -d '\r\n' | \
  gcloud secrets versions add lqabr-vapi-api-key --project=ldqfingsrv-dev --data-file=-
```

> **Note.** `LQABR_VAPI_PHONE_NUMBER_ID` and `LQABR_VAPI_ASSISTANT_ID` are **not** secrets —
> they are already committed in plaintext at `agents/text_voice/setup_env.sh:45-46`. They
> belong in `--set-env-vars` at deploy time, not in Secret Manager.

**Executed 2026-08-26 — succeeded:**
```
Created secret [lqabr-vapi-api-key].
Created version [1] of the secret [lqabr-vapi-api-key].
```

> **ROTATE THIS KEY.** The Vapi API key value was pasted into a chat transcript during this
> run before the pipe-from-`.env` method was adopted. Rotate it in the Vapi dashboard once
> the build is verified. Rotation only needs a new secret **version** — no redeploy, because
> `--set-secrets` with the `:latest` alias re-reads at container start.

### 3. Validation

**NOT YET RUN.**

> ### ⚠ CORRECTION — "exists" is not the right check
> This phase was first recorded as done because `gcloud secrets list` showed the names.
> **Listing a secret says nothing about whether it holds a version.** Six of the fourteen
> `lqabr-*` secrets in this project are empty shells with zero versions:
>
> | Secret | Enabled versions | Impact |
> | --- | --- | --- |
> | `lqabr-hubspot-webhook-secret` | **0** | **blocks the gateway** — see below |
> | `lqabr-zoom-account-id`, `-client-id`, `-client-secret`, `-webhook-secret-token` | 0 | none — dormant (D3) |
> | `lqabr-zoominfo-username`, `-password` | 0 | none — dormant (D3) |
>
> Always validate on **versions**, never on names.

**Confirm every secret that is actually used has an ENABLED version:**
```bash
for s in $(gcloud secrets list --project=ldqfingsrv-dev --format='value(name)'); do
  n=$(gcloud secrets versions list $s --project=ldqfingsrv-dev \
        --filter='state:ENABLED' --format='value(name)' 2>/dev/null | wc -l)
  printf "%-38s enabled_versions=%s\n" "$s" "$n"
done
```

The six secrets this build depends on must all report `enabled_versions=1`.

**Confirm the new secret has an enabled version — without printing the value:**
```bash
gcloud secrets versions list lqabr-vapi-api-key --project=ldqfingsrv-dev \
  --format='table(name,state,createTime)'

gcloud secrets versions access 1 --secret=lqabr-vapi-api-key \
  --project=ldqfingsrv-dev | wc -c
```

Expect version `1` in state `ENABLED`, and a byte count of **36** (the UUID-form key length).
A count of **37 means a trailing newline survived** the `tr -d '\r\n'` — that breaks the Vapi
API call in a way that is irritating to diagnose, because the key looks correct everywhere it
is printed.

**End-to-end — the check that actually proves P5:**
```bash
cd agents/text_voice && ./setup_env.sh
```

This fetches `lqabr-vapi-api-key` **and** `lqabr-anthropic-api-key` from Secret Manager and
rewrites `.env`, hard-failing if either is missing. A clean run proves the secrets are
readable by a real consumer, not merely that the objects exist. It backs up the existing
`.env` to `.env.bak` first.

---

## P6 — VPC, subnet, router, static egress IP, Cloud NAT

**Status: DONE + VALIDATED (2026-08-26)**

> ### ⚑ EGRESS IP — `34.45.4.100`
> Allowlist this single address at **HubSpot, Mailgun and Vapi**. All seven services share
> it; the vendor cannot distinguish which component called.

### 0. Network model — read this before the commands

Cloud Run is **not** a VM/Kubernetes network. Getting this wrong leads to a plausible but
incorrect mental model, so it is stated explicitly:

| Common assumption | Reality on Cloud Run |
| --- | --- |
| Inbound arrives via the NAT public IP | **Cloud NAT is outbound-only.** It cannot forward inbound traffic. HubSpot never touches `lqabr-egress-ip` |
| The gateway lives in a "public subnet", agents in a "private subnet" | There are no public/private subnets. **All seven services attach to the same subnet.** What differs is `--ingress` and IAM, not network placement |
| Each container has a routable IP and port | Instances are ephemeral and scale to zero. You address a **service URL**, never an instance |
| The VPC carries both directions | In this design the subnet is **egress-only**. Ingress never enters through it |

**The two independent paths:**

| Direction | Path | Address seen by the far end |
| --- | --- | --- |
| **Ingress** — HubSpot → gateway | Google's global frontend terminates TLS at `https://lqabr-dev-gtwy-<hash>-uc.a.run.app`. Permitted by `--ingress=all`. The VPC plays no part | Google frontend |
| **Egress** — any service → Google APIs (Secret Manager, Logging, Artifact Registry, Monitoring) | Private Google Access flag on the subnet. **One flag, not an endpoint; covers all Google APIs for all services; $0** | never leaves Google's network |
| **Egress** — any service → HubSpot, Mailgun, Vapi | Cloud NAT | `lqabr-egress-ip` (**public** static IP — allowlist this at each vendor) |

> **UNRESOLVED — the gateway→agent hop (P0-I9).** Whether `*.run.app` traffic from the
> gateway is treated as *internal* by an `ingress=internal` agent depends on how egress is
> scoped and whether `run.app` resolves through a private DNS zone. With
> `--vpc-egress=all-traffic` plus Cloud NAT and no private DNS zone, the call plausibly
> exits to a public IP and is rejected by `ingress=internal` — every dispatch would 403.
> The candidate fixes are (a) scope the gateway to `--vpc-egress=private-ranges-only`, or
> (b) add a private DNS zone routing `run.app` to the restricted VIP `199.36.153.4/30`.
> **This must be proven empirically at P9 with ONE agent before all seven deploy.** Do not
> assume either behaviour.

### 1. End results

| # | Resource | Notes |
| --- | --- | --- |
| 1 | `lqabr-vpc` | custom-mode VPC |
| 2 | `lqabr-run-uscentral1` | subnet, `us-central1`, `10.10.0.0/23`, Private Google Access ON |
| 3 | `lqabr-router` | Cloud Router |
| 4 | `lqabr-egress-ip` | reserved regional static IP — **$3.65/mo, the only cost in P6** |
| 5 | `lqabr-nat` | Cloud NAT bound to that IP — **$0 at scale-to-zero** |

### 2. Commands

Run in order — each depends on the previous.

```bash
gcloud compute networks create lqabr-vpc \
  --subnet-mode=custom --project=ldqfingsrv-dev

gcloud compute networks subnets create lqabr-run-uscentral1 \
  --network=lqabr-vpc --region=us-central1 --range=10.10.0.0/23 \
  --enable-private-ip-google-access --project=ldqfingsrv-dev

gcloud compute routers create lqabr-router \
  --network=lqabr-vpc --region=us-central1 --project=ldqfingsrv-dev

gcloud compute addresses create lqabr-egress-ip \
  --region=us-central1 --project=ldqfingsrv-dev

gcloud compute routers nats create lqabr-nat \
  --router=lqabr-router --region=us-central1 \
  --nat-external-ip-pool=lqabr-egress-ip --nat-all-subnet-ip-ranges \
  --project=ldqfingsrv-dev
```

**Two deviations from `SETUP_CLOUDRUN_IAM.md` §2.7:**

| Deviation | Reason |
| --- | --- |
| `--enable-private-ip-google-access` added | The doc omits it. Without it, services cannot reach Secret Manager / Logging / Artifact Registry over private IP; that traffic is forced out through NAT and back, which is slower, incurs NAT data-processing charges, and is the usual cause of "why can't my Cloud Run service read its secret" |
| `/23` instead of `/24` | Direct VPC egress consumes a subnet IP **per instance**, and during a rolling deploy old and new revisions coexist. `/24` = 252 usable, `/23` = 510. Resizing later requires detaching and redeploying **all seven services** — free now, expensive later |

> **Note.** The NAT create is the slowest step (~30s). Capture the allocated address from
> step 4 — that is the IP to allowlist at HubSpot, Mailgun and Vapi.

### 3. Validation

```bash
gcloud compute networks list --project=ldqfingsrv-dev

gcloud compute networks subnets describe lqabr-run-uscentral1 \
  --region=us-central1 --project=ldqfingsrv-dev \
  --format='value(name,ipCidrRange,privateIpGoogleAccess)'

gcloud compute addresses describe lqabr-egress-ip \
  --region=us-central1 --project=ldqfingsrv-dev \
  --format='value(name,address,status)'

gcloud compute routers nats describe lqabr-nat --router=lqabr-router \
  --region=us-central1 --project=ldqfingsrv-dev \
  --format='value(name,natIpAllocateOption,sourceSubnetworkIpRangesToNat)'
```

**Verified 2026-08-26:**

```
lqabr-run-uscentral1    10.10.0.0/23    True
lqabr-egress-ip         34.45.4.100     IN_USE
```

`privateIpGoogleAccess: True` is the line that matters — the subnet *create* output does not
display it, so this describe is the only real confirmation. `IN_USE` (rather than `RESERVED`)
confirms Cloud NAT actually bound the address.

### 4. Firewall posture — deliberate, and a recorded gap

`gcloud compute networks create` prints a warning that "instances on this network will not be
reachable until firewall rules are created." **That warning is VM boilerplate and does not
apply here.** No firewall rules were created, and none are required:

| | Rule | Effect on this design |
| --- | --- | --- |
| Implied | allow-all **egress** (priority 65535) | Cloud Run Direct VPC egress only ever *originates* traffic; the firewall is stateful, so return packets flow |
| Implied | deny-all **ingress** (priority 65535) | Irrelevant. Nothing dials in over VPC IPs — a service is reached by its `run.app` URL, never a subnet IP |

**Inbound does not traverse the VPC firewall at all.** HubSpot reaches the gateway through
Google's global frontend; `--ingress=all` on the *service* is the control, not a firewall
rule. This is why the unknown/unstable vendor IP ranges do not matter for inbound.

Verified: `gcloud compute firewall-rules list --filter='network:lqabr-vpc'` → `Listed 0 items.`

> **OPEN GAP — egress is unrestricted.** The implied allow-all-egress rule means any agent
> can reach **any** internet destination, not only HubSpot / Mailgun / Vapi / Anthropic. In a
> design whose stated premise is blast-radius containment, a compromised agent has
> unrestricted outbound. Neither `SETUP_CLOUDRUN_IAM.md` nor its §5 threat table mentions
> this.
>
> Not remediated in this run, deliberately: HubSpot, Mailgun, Vapi and Anthropic do **not**
> publish stable IP ranges, so IP-based allowlisting is fragile and fails silently when a
> vendor rotates infrastructure. The robust form is Secure Web Proxy or VPC-SC with FQDN
> policy — more machinery than this sandbox warrants. **Recorded as a hardening item for
> production.**

### 5. Outbound path summary (as built)

| Source | Destination | Path | Address the far end sees |
| --- | --- | --- | --- |
| all 7 services | Secret Manager, Logging, Artifact Registry, Monitoring | Private Google Access | never leaves Google's network |
| `mcp` | HubSpot API | Cloud NAT | `34.45.4.100` |
| `email` | Mailgun | Cloud NAT | `34.45.4.100` |
| `txtv` | Vapi | Cloud NAT | `34.45.4.100` |
| `summary`, `research`, `txtv` | Anthropic API | Cloud NAT | `34.45.4.100` |

---

## P7 — Deployer group

**Status: VERIFIED / SKIPPED (2026-08-26) — already bound, nothing applied**

### 1. End results

The deployer tier is `group:ai2d@aidefinitive.com` — a **human Google group** whose members
can build, push and deploy Cloud Run services, but cannot enable APIs or change IAM.

| Role | Purpose | Scope | Per doc §2.6? |
| --- | --- | --- | --- |
| `roles/run.developer` | deploy Cloud Run services | project | yes |
| `roles/cloudbuild.builds.editor` | run Cloud Build | project | yes |
| `roles/artifactregistry.writer` | push images | project | yes |
| `roles/secretmanager.viewer` | see secret names / metadata | project | yes |
| `roles/logging.viewer` | read logs | project | yes |
| `roles/monitoring.viewer` | read metrics | project | yes |
| `roles/serviceusage.serviceUsageConsumer` | consume project APIs | project | extra — benign |
| `roles/secretmanager.secretAccessor` | **read secret VALUES** | project | extra — **deviation, see D9 / P4** |
| `roles/iam.serviceAccountUser` | `actAs` the runtime SA at deploy time | **on the SA**, not the project | yes |

The `actAs` binding is the only intended link between the deployer and runtime tiers — no
group membership, no shared roles.

> **Note on scope.** `run.developer` and `artifactregistry.writer` are project-wide by
> construction; there is no resource-scoped alternative. This was a decisive argument
> against the doc's original target `leadgen-snbox-11b7c` (see D1) — there, the same
> bindings would have let the deployer group delete unrelated Firebase/Stripe Cloud Run
> services and overwrite images in `gcf-artifacts`. In `ldqfingsrv-dev` the project contains
> only LQABR, so project-wide scope is contained.

> **Note on `run.developer` and public bindings.** `roles/run.developer` does **not** include
> `run.services.setIamPolicy`. The deployer therefore cannot apply
> `--allow-unauthenticated` on the gateway; that binding must be applied separately by
> `swaroop@` at P9. Confirmed non-blocking:
> `constraints/iam.allowedPolicyMemberDomains` is `allValues: ALLOW` on org 621891143198,
> so the `allUsers` binding is permitted.

### 2. Commands

None applied — every binding above already existed from the August 2026 work.

### 3. Validation

```bash
gcloud projects get-iam-policy ldqfingsrv-dev --flatten='bindings[].members' \
  --filter='bindings.members:ai2d@aidefinitive.com' --format='value(bindings.role)'

gcloud iam service-accounts get-iam-policy \
  lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com \
  --project=ldqfingsrv-dev --format='value(bindings.role,bindings.members)'
```

Verified 2026-08-26: 8 project roles as tabulated, and
`roles/iam.serviceAccountUser → ['group:ai2d@aidefinitive.com']` on the runtime SA.

---

## P8a — Auth & routing probe (MCP deployed first, deliberately)

**Status: ALL THREE STAGES PASSED (2026-08-26). P0-I9 RESOLVED — no egress change needed.**

### 0. Why this exists

P8 rewrites five call sites to attach ID tokens. Whether that works depends on an assumption
neither `SETUP_CLOUDRUN_IAM.md` nor Google's docs settle for this exact shape: **is
`*.run.app` traffic from one Cloud Run service seen as _internal_ by an `ingress=internal`
callee, given `--vpc-egress=all-traffic` and Cloud NAT?** Writing five call sites against an
unverified assumption risks rewriting all five. MCP is the right probe — it is the callee in
three of the five hops and has no dependencies of its own.

| Stage | Config | Proves | Result |
| --- | --- | --- | --- |
| 1 | `ingress=all` + `no-allow-unauthenticated` | IAM auth | **PASS** |
| 2 | `ingress=internal` | network isolation | **PASS** |
| 3 | call from inside the VPC | the routing question (P0-I9) | **PASS** |

### 1. End results

- Cloud Run service **`lqabr-dev-mcp`** deployed, revision `lqabr-dev-mcp-00002-76g`.
- URL: `https://lqabr-dev-mcp-432617526728.us-central1.run.app`
- `ingress=internal`, `no-allow-unauthenticated`, on `lqabr-vpc` / `lqabr-run-uscentral1`.
- Runtime SA `lqabr-agent-dev`; HubSpot token injected as `HUBSPOT_PRIVATE_APP_TOKEN`.

### 2. Commands

**Stage 1 — deploy public-ingress but auth-required** (internet-reachable, never public):

```bash
gcloud run deploy lqabr-dev-mcp \
  --image=us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-mcp:0.1.0 \
  --region=us-central1 --project=ldqfingsrv-dev \
  --service-account=lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com \
  --execution-environment=gen2 \
  --network=lqabr-vpc --subnet=lqabr-run-uscentral1 --vpc-egress=all-traffic \
  --ingress=all --no-allow-unauthenticated \
  --set-secrets=HUBSPOT_PRIVATE_APP_TOKEN=lqabr-hubspot-access-token:latest \
  --port=8080 --max-instances=3
```

**Stage 2 — flip to internal:**

```bash
gcloud run services update lqabr-dev-mcp \
  --region=us-central1 --project=ldqfingsrv-dev --ingress=internal
```

**Stage 3 — probe from inside the VPC.** A Cloud Run **job** reusing the MCP image with its
entrypoint overridden, so no code and no extra image are needed. It runs with the same SA,
subnet and egress setting a real agent will use.

```bash
PROBE='import urllib.request as u
a="https://lqabr-dev-mcp-432617526728.us-central1.run.app"
t=u.urlopen(u.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience="+a,headers={"Metadata-Flavor":"Google"})).read().decode()
print("TOKEN_LEN",len(t))
try:
    r=u.urlopen(u.Request(a+"/mcp",data=b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}",headers={"Authorization":"Bearer "+t,"Content-Type":"application/json","Accept":"application/json, text/event-stream"}))
    print("PROBE_STATUS",r.status)
    print(r.read()[:400])
except Exception as e:
    print("PROBE_ERR",type(e).__name__,e)'

gcloud run jobs create mcp-probe \
  --image=us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-mcp:0.1.0 \
  --region=us-central1 --project=ldqfingsrv-dev \
  --service-account=lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com \
  --network=lqabr-vpc --subnet=lqabr-run-uscentral1 --vpc-egress=all-traffic \
  --command=python --args="^@^-c@$PROBE" \
  --max-retries=0 --task-timeout=120s

gcloud run jobs execute mcp-probe --region=us-central1 --project=ldqfingsrv-dev --wait
```

> **Note.** `^@^` tells gcloud to split `--args` on `@` rather than commas, because the
> Python contains commas.

### 3. Validation

**Stage 1 — verified 2026-08-26:**

```bash
URL=https://lqabr-dev-mcp-432617526728.us-central1.run.app
curl -s -o /dev/null -w "%{http_code}\n" $URL/mcp                                    # -> 403
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" $URL/mcp             # -> 406
```

| Check | Result | Why it is the right answer |
| --- | --- | --- |
| unauthenticated | **403** | appears in the Cloud Run request log but **not** in uvicorn's — Google's frontend rejected it before the container saw it |
| with ID token | **406** | appears in **both** logs (`"GET /mcp HTTP/1.1" 406`) — the request reached the app; FastMCP simply rejects a bare `GET` without the right `Accept` header. Auth passed |

Container startup was clean — `FastMCP 3.4.7`, server `lqabr-hubspot` 0.1.0,
streamable-http on `/mcp`, `Application startup complete`, and **no** `AuthConfigError` or
`SecretConfigError`. That absence also confirms `HUBSPOT_PRIVATE_APP_TOKEN` is the correct
variable name — a wrong name would have surfaced here.

**Stage 2 — verified 2026-08-26:** the same token that returned 406 now returns **404**.
Cloud Run answers internal-ingress requests from outside the VPC with 404 rather than 403 —
it hides the service instead of admitting it exists. Network isolation now sits on top of IAM.

**Stage 3 — PASSED 2026-08-26.** Read the result with:

```bash
gcloud run jobs executions logs read \
  $(gcloud run jobs executions list --job=mcp-probe --region=us-central1 \
    --project=ldqfingsrv-dev --limit=1 --format='value(name)') \
  --region=us-central1 --project=ldqfingsrv-dev
```

| Output | Meaning |
| --- | --- |
| `PROBE_STATUS 200` | path works — P8 is safe to write against `--vpc-egress=all-traffic` |
| `PROBE_ERR HTTPError ... 404` | **P0-I9 confirmed** — `run.app` is being NAT'd out and rejected. Switch the caller to `--vpc-egress=private-ranges-only`, or add a private DNS zone routing `run.app` to the restricted VIP `199.36.153.4/30` |
| `PROBE_ERR HTTPError ... 403` | routing is fine; an IAM problem instead |
| no `TOKEN_LEN` line | metadata server unreachable — a different failure entirely |

**Stage 3 actual output:**

```
TOKEN_LEN 821
PROBE_ERR HTTPError HTTP Error 500: Internal Server Error
```

MCP's own request log for the same moment:

```
2026-08-26 16:45:53  POST 500  https://lqabr-dev-mcp-432617526728.us-central1.run.app/mcp
```

| Signal | Meaning |
| --- | --- |
| `TOKEN_LEN 821` | metadata server reachable; ID token minted with the correct audience |
| **not 404** | the `ingress=internal` service **accepted** the request — routing from inside the VPC works |
| **not 403** | IAM accepted the token — the runtime SA's project-wide `run.invoker` is sufficient |
| `POST 500` in MCP's log | the request reached the application itself |

> ### ✅ P0-I9 RESOLVED — `--vpc-egress=all-traffic` does NOT break the internal mesh
> The concern was that `*.run.app` traffic would be NAT'd out to a public IP and rejected by
> `ingress=internal`. **Empirically it is not.** No private DNS zone for `run.app` is needed,
> and the gateway does not need `--vpc-egress=private-ranges-only`. The original
> `SETUP_CLOUDRUN_IAM.md` §3 `COMMON` block is correct on this point; the architect-critic's
> objection, while reasonable, does not hold for this configuration.
>
> **P9 therefore keeps `--vpc-egress=all-traffic` on all services.** The two *other* P9
> corrections (gateway `--max-instances=1`, and `LQABR_MCP_BASE_URL` for txtv) still stand.

**The 500 is a protocol error, not an infrastructure one.** MCP streamable-http requires an
`initialize` handshake to establish a session before `tools/list` is valid; the probe called
`tools/list` cold. The P8 client code performs the handshake properly. The 500 is therefore
the *expected* response to a deliberately minimal probe — what mattered was that it was 500
and not 404 or 403.

> **Note — the job also hit its 120s task timeout** after printing its result, and Cloud Run
> reported `failedCount=1`. The probe's output was already emitted; the timeout is an
> artifact of the one-liner not exiting cleanly and has no bearing on the finding.

**Clean-up — done 2026-08-26:**
```bash
gcloud run jobs delete mcp-probe --region=us-central1 --project=ldqfingsrv-dev --quiet
```
`Deleted job [mcp-probe].`

---

## P8 — Code: ID-token authentication for private callees

**Status: DONE (2026-08-26) — manual verification only; see the test-suite gap below.**

### 1. End results

Every caller of a private Cloud Run service attaches a Google-signed ID token with
`audience=<callee URL>`, applied **only** when the target is `https://*.run.app`, so local
loopback is unchanged.

| File | Hop | Change |
| --- | --- | --- |
| `packages/lqabr_core/lqabr_core/gcp_id_token.py` | — | **new** — for `txtv`, `email`, `ldpf` |
| `agents/gateway/lib/soloai/id_token.py` | — | **new** — copy (gateway forbids `lqabr_core`) |
| `agents/research/packages/research_core/gcp_id_token.py` | — | **new** — copy (research is standalone) |
| `agents/summary/packages/summary_core/gcp_id_token.py` | — | **new** — copy (summary is standalone) |
| `agents/gateway/lib/soloai/protocols/a2a.py` | gateway → agents | `**auth_header(endpoint)` in the headers dict |
| `agents/research/packages/research_core/mcp/client.py` | research → MCP | `**auth_header(...)` in `_headers()` |
| `agents/summary/packages/summary_core/mcp/client.py` | summary → MCP | `**auth_header(...)` in `_headers()` |
| `agents/text_voice/src/mcp_client.py` | txtv → MCP | `**auth_header(self._base_url)` in `_headers()` |
| `agents/gateway/src/call_report.py` | gateway → txtv relay | **NO CHANGE NEEDED** |

> **CORRECTION to the earlier plan.** This was scoped as *five* call sites. It is **four**.
> `agents/gateway/src/call_report.py` already contains a complete `IdTokenProvider`
> (lines ~145-190) that mints a token from the metadata server, caches it with a TTL,
> uses the service **root** as the audience (not the path), and returns `None`
> off-platform. The original survey searched for `fetch_id_token` / `IDTokenCredentials`
> and missed a hand-rolled implementation. It is correct as written; it was not touched.

### 2. Commands

Source edits only — no gcloud.

#### Design decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Token source | **stdlib `urllib` → metadata server** | Adding `google-auth` would introduce a dependency to *both* the gateway (currently 5 packages) and `lqabr_core` (which ships none of Google's auth libraries). The P8a probe proved this exact metadata call works from inside the VPC. |
| Placement | **four copies** | Three units forbid `lqabr_core` by explicit rule: the gateway (`requirements.txt:7`), `research_core` and `summary_core` (each has a `tests/test_standalone.py` with a `FORBIDDEN_ROOTS` set that fails the build on such an import). Duplicating ~40 lines honours those boundaries. All four files cross-reference each other. |
| Applied when | target is `https://*.run.app` only | loopback and vendor URLs (`api.hubapi.com`, Mailgun, Vapi) get nothing — verified |
| On failure | return `{}`, never raise | off-platform there is no metadata server; a hard failure would break every local run to serve a cloud-only concern |
| Caching | per-audience, 2700s TTL | tokens last ~1h; re-minting per request would add a metadata round-trip to every hop |
| Precedence | an existing static `Authorization` wins | research/summary already support `settings.mcp_auth_token`; existing behaviour is unchanged |

Public surface:

```python
def auth_header(url: str) -> dict:
    """{'Authorization': 'Bearer <id-token>'} for run.app targets, else {}."""
```

Safe to splat unconditionally: `headers = {..., **auth_header(url)}`.

#### Also fixed here

`agents/text_voice/src/mcp_client.py:23` reads **`LQABR_MCP_BASE_URL`**. `SETUP_CLOUDRUN_IAM.md`
§3 sets `LQABR_TXTV_MCP_BASE_URL`, which the code never reads — txtv would silently fall back
to `http://localhost:8080/mcp` and point at itself. **P9 must set `LQABR_MCP_BASE_URL`.**

> ### ⚠ DEFECT INTRODUCED AND FIXED IN THIS PHASE
> The first cut of P8 added `from lqabr_core.gcp_id_token import auth_header` to
> **`research_core/mcp/client.py`** and **`summary_core/mcp/client.py`**. Both agents are
> standalone by explicit design and each ships a `tests/test_standalone.py` that fails on
> exactly that import:
>
> * research — `FORBIDDEN_ROOTS = {"lqabr_core", "summary_core", "mcp", "agents", "packages"}`
> * summary — `FORBIDDEN_ROOTS = {"lqabr_core", "mcp"}`, whose own guidance reads
>   *"copy what you need from lqabr_core into (summary_core.mcp)"*
>
> Fixed by giving each package its own copy and repointing the imports to
> `research_core.gcp_id_token` / `summary_core.gcp_id_token`.
>
> **Caught while preparing P9, not by the test suite.** Those `test_standalone.py` files
> are among the 14 that do not collect from the root runner — so the root suite could
> never have flagged it. This is the concrete cost of the KNOWN GAP below.
>
> Verified after the fix by running each agent's suite from its own directory:
> `research → test_no_repo_imports PASSED`; `summary → 39 passed`.
> (One pre-existing research failure, `test_the_edge_still_accepts_both_spellings`, is
> unrelated — it inspects `service_app.py`, which was not touched.)

### 3. Validation

**Syntax — all six files compile:**
```bash
for f in packages/lqabr_core/lqabr_core/gcp_id_token.py agents/gateway/lib/soloai/id_token.py \
         agents/gateway/lib/soloai/protocols/a2a.py \
         agents/research/packages/research_core/mcp/client.py \
         agents/summary/packages/summary_core/mcp/client.py \
         agents/text_voice/src/mcp_client.py; do python3 -m py_compile "$f"; done
```

**Behaviour — verified 2026-08-26 off-platform:**

| Input | Result | Correct because |
| --- | --- | --- |
| `http://localhost:8091/mcp` | `{}` | loopback must stay unauthenticated |
| `https://lqabr-dev-mcp-...run.app/mcp` | audience `https://lqabr-dev-mcp-...run.app` | path stripped — Cloud Run validates against the service root |
| `https://api.hubapi.com/x` | `''` | **vendor calls must never carry a Google token** |
| `run.app` header, no metadata server | `{}` | graceful off-platform degradation |

**Regression — zero new failures.** Verified by removing P8 entirely (stashing the four edits
and moving the two new files aside), capturing the error set, restoring, and diffing:

```
baseline errors: 14  |  with P8: 14
diff -> IDENTICAL — no new failures from P8
```

> ### ⚠ KNOWN GAP — the root test suite does not collect
>
> `python3 -m pytest -c tests/pytest.ini -q` fails with **14 collection errors, and did so
> before this run began.** CLAUDE.md §11.3's "64 tests green" baseline is **stale**. This
> also means the Definition of Done ("full suite passes from the root") cannot currently be
> met by any change.
>
> Root causes:
>
> | Cause | Detail |
> | --- | --- |
> | Colliding basenames | `schema.py` and `pipeline.py` exist in **both** `agents/research/src` and `agents/summary/src`; `service_app.py` in email, research and summary |
> | Bare imports | summary/research tests do `import schema`, so whichever `src/` is first on `sys.path` wins — silently |
> | Conftest-level failure | `agents/research/tests/conftest.py` imports `research_core` at module level, so the whole directory errors |
> | Dead test | `tests/integration/test_orchestrator_triggers_email_agent.py` imports `orchestrator_agent`, which **does not exist** in this repo |
>
> **A `pythonpath` entry in `tests/pytest.ini` does NOT fix this — it was tried and made
> things worse (14 → 17 errors), newly breaking `agents/text_voice/tests/test_tools.py`.**
> Adding directories to a shared `sys.path` creates new shadowing. The change was reverted;
> `tests/pytest.ini` is unmodified.
>
> The agents that *do* work dodge the collision deliberately —
> `agents/text_voice/tests/conftest.py` loads modules by file path under unique names,
> and says why: *"several agents in this repo ship modules with the same basename."*
> The summary and research suites (added 2026-08-23) were written to run from their own
> directories and never adapted to the root runner.
>
> **Options for a real fix**, none of them a config tweak:
> 1. **Convert summary/research tests to the `text_voice` pattern** — load by path under
>    unique module names. Matches the established convention. *Recommended.*
> 2. **Make each `src/` a real package** (`summary_src`, `research_src`). Cleaner long-term,
>    larger blast radius.
> 3. **Run per-agent suites separately** and drop the single-root-command claim. Least work,
>    weakens the DoD gate.
>
> Plus deleting or skipping the dead orchestrator test either way.
>
> **Consequence for P8: it has manual verification only.** The four edits have no automated
> safety net. No unit tests were written for `gcp_id_token.py` — deferred pending the
> decision above.

---

## P9 — Build, push and deploy the services

**Status: IN PROGRESS — `mcp` deployed (P8a), `summary` image built. Gateway sequenced last.**

### 0. Deployment order

The gateway is **deliberately last**: it is blocked on the empty
`lqabr-hubspot-webhook-secret` (see P5), and it is also the only public entry point — there
is nothing to route to until the agents behind it are up.

| # | Service | Image | State |
| --- | --- | --- | --- |
| 1 | `lqabr-dev-mcp` | promoted from Docker Hub | **deployed** (P8a) — private, verified |
| 2 | `lqabr-dev-summary` | own Dockerfile, context = agent dir | **DEPLOYED + VERIFIED** — rev `00003-v5j`, `mcp_startup_check_ok` |
| 3 | `lqabr-dev-research` | own Dockerfile, context = agent dir | not built |
| 4 | `lqabr-dev-email-agent` | shared `infra/gcp/cloud-run/Dockerfile` | stale (2026-08-03) |
| 5 | `lqabr-dev-txtv` | own Dockerfile | stale (2026-08-04) |
| 6 | `lqabr-dev-ldpf` | shared Dockerfile, `SERVICE_KIND=agent` | stale (2026-08-04) |
| 7 | **`lqabr-dev-gtwy`** | own Dockerfile | **LAST — blocked on the HubSpot secret** |

> **Naming.** Every service follows `lqabr-dev-<component>`. `summary` was realigned from
> `lqabr-summary-agent` on 2026-08-26, before its first deploy, while renaming was still
> free — a Cloud Run rename means a new URL.

### 0.1 `lqabr-dev-summary` — deployed 2026-08-26

**URL:** `https://lqabr-dev-summary-432617526728.us-central1.run.app` ·
`ingress=internal`, `no-allow-unauthenticated`, on `lqabr-vpc`, SA `lqabr-agent-dev`.

> ### ✅ P8 PROVEN IN PRODUCTION
> ```
> mcp_initialized
> mcp_tools_discovered
> mcp_startup_check_ok
> ```
> A private agent authenticated to a private MCP with a metadata-server ID token and
> completed the MCP handshake. This is the full P8 path working for real — the P8a probe
> only proved the network hop, not the client code.

**Deploy-script changes required (`agents/summary/infra/`):**

| Change | Why |
| --- | --- |
| `SERVICE_NAME` → `lqabr-dev-summary` | was `lqabr-summary-agent`; realigned to the live `lqabr-dev-<component>` convention **before** first deploy, while a rename was still free |
| `RUNTIME_SA` → `lqabr-agent-dev@` | was `lqabr-agent-runtime@`, which does not exist |
| `LQABR_SUMMARY_MCP_BASE_URL` → real MCP URL | was a guessed hostname |
| `--network/--subnet/--vpc-egress` added | **without these the service is not on the VPC and cannot reach the `ingress=internal` MCP at all** |
| `--ingress internal` added | it would otherwise have been internet-reachable |
| `--execution-environment gen2` added | matches the rest of the fleet |
| `BUILD_SA` added | Cloud Build needs a user-managed SA (P3) |

#### Two defects found by deploying — both pre-existing, neither caught by any test

**1. `IndexError` at import — `settings.py` assumed the repo layout.**

```
File "/app/packages/summary_core/settings.py", line 61, in _resolve_log_file
  root = Path(__file__).resolve().parents[4]
IndexError: 4
```

In the repo the module sits at `<repo>/agents/summary/packages/summary_core/settings.py`, so
`parents[4]` is the repo root. The Dockerfile flattens `agents/summary` to `/app`, leaving
only `/app/packages/summary_core/` — two levels shallower. Because the statement runs at
**module import** (`service_app.py:122` → `get_settings`), the container died before binding
the port and Cloud Run reported only its generic *"failed to start and listen on
PORT=8080"*, which points nowhere near a path calculation.

Fixed in both agents by falling back to the agent root when the repo layout is absent:

```python
_here = Path(__file__).resolve(); _up = _here.parents
root = _up[4] if len(_up) > 4 and _up[3].name == "agents" else _up[2]
```

> **`research_core/settings.py:60` had the identical module-level `parents[4]`** and was
> fixed at the same time — it would have failed the same way on its first deploy, and the
> blame would plausibly have landed on its newly written Dockerfile instead.

**2. MCP tool names were stale.**

```
the MCP ... does not expose ['post_patch_crm', 'get_lead_profile_details']
 — it offers ['get_blog_summary', 'get_lead_profile', 'upsert_blog_summary', 'upsert_lead_profile']
```

MCP is the correct side — those four match its published contract. `summary_core`'s defaults
(`settings.py:194-196`) were never updated. Config-only fix, no code change, and no rebuild
(an env change alone mints a new Cloud Run revision):

```bash
export LQABR_SUMMARY_MCP_TOOL_READ="get_blog_summary"
export LQABR_SUMMARY_MCP_TOOL_WRITE="upsert_blog_summary"
```

Summary works on blog summaries on tickets (`OBJECT_TYPE=ticket`,
`SUMMARY_PROPERTY=blog_summary`), so it reads and writes the blog-summary pair.

> **Still open for summary:** `LQABR_SUMMARY_DRY_RUN=1` — it will not write to HubSpot until
> that is set to `0` and redeployed.

### 0.2 End-to-end verification — summary wrote to HubSpot

**2026-08-26.** A live run through the full private path:

```json
{"ticket_hs_id": "330652551872", "action": "created", "blog_industry": "HEALTHCARE",
 "status": "written", "summary_ref_id": "summary-d847c642-c5d1-4010-b26e-ec747df9bbb6"}
```

Path exercised: **job in the VPC → ID token → private summary → ID token → private MCP →
HubSpot API → out via Cloud NAT `34.45.4.100`.** `action: "created"` confirms the MCP
creates the ticket itself, keyed on `blog_published_at` — **no `object_id` is ever needed**,
contrary to the first reading of `HubSpotTarget`.

#### How to invoke it (the service is `ingress=internal`, so a laptop curl gets 404)

A Cloud Run job in the VPC, rather than flipping ingress — the service stays private:

```bash
PROBE='import urllib.request as u, json
a="https://lqabr-dev-summary-432617526728.us-central1.run.app"
t=u.urlopen(u.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience="+a,headers={"Metadata-Flavor":"Google"})).read().decode()
body=json.dumps({"source":"<BLOG_URL>","hubspot":{"blog_published_at":"2026-08-20T23:01:00.000Z","industry":"HEALTHCARE"}}).encode()
r=u.urlopen(u.Request(a+"/summary/run",data=body,headers={"Authorization":"Bearer "+t,"Content-Type":"application/json"}),timeout=540)
print("STATUS",r.status,flush=True); print(r.read().decode()[:4000],flush=True)'

gcloud run jobs create summary-run --image=us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-mcp:0.1.0 \
  --region=us-central1 --project=ldqfingsrv-dev \
  --service-account=lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com \
  --network=lqabr-vpc --subnet=lqabr-run-uscentral1 --vpc-egress=all-traffic \
  --command=python --args="^@^-c@$PROBE" --max-retries=0 --task-timeout=600s

gcloud run jobs execute summary-run --region=us-central1 --project=ldqfingsrv-dev --wait
```

> **Do not delete the job before it finishes.** A first attempt was deleted 27s in — before
> the container had made its HTTP call — leaving no output and no request at summary.
> Cold start plus model call is 1-3 minutes.

#### Request contract

| Field | Rule |
| --- | --- |
| `source` | URL or raw text; a bare string works |
| `hubspot.blog_published_at` | **full ISO 8601, must contain `T`** — this is the upsert key. A bare `YYYY-MM-DD` is auto-expanded; `2026-08-20:23:01:00` passes through **malformed and silently no-ops** |
| `hubspot.industry` | the portal enum. Now normalised automatically (see below) |
| `hubspot.subject` | optional — defaults to the model's title |
| `hubspot.object_id` | **not used** in `blog_summary` style |

Re-running the same `blog_published_at` **updates** that ticket (`action` → `updated`), so
iterating is safe.

> **Verify with a `timestamp>` filter, never `--freshness`.** Stale entries inside the
> freshness window twice looked like the current result during this run.

#### Four failures, in the order they were hit

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | `hubspot_write_dry_run` | `config.sh` defaulted `DRY_RUN=1`, so a plain redeploy silently reverted it | default flipped to `0` |
| 2 | `[Errno 13] Permission denied: 'errors'` | the MCP image runs as user **`mcp`** with `WorkingDir=/app`, which is root-owned; it writes an `errors` path at tool-call time | in-memory volume mounted at `/app/errors` |
| 3 | `blog_industry 'Healthcare' is not one of ['FINANCIAL_SERVICES','LEGAL_SERVICES','HEALTHCARE']` | model prose vs portal enum | normalisation, below |
| 4 | `SecretConfigError: cannot resolve secret 'HUBSPOT_PRIVATE_APP_TOKEN'` | **`--set-secrets=HUBSPOT_PRIVATE_APP_TOKEN=…` is NOT how this image takes its token.** With `HUBSPOT_AUTH_MODE=private_app` baked in it resolves through its own `LQABR_SECRET_*` layer | set `LQABR_SECRET_PROJECT` + `LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN` |

> **CORRECTION — `SETUP_CLOUDRUN_IAM.md` §3 was right about the token variables.** This run
> initially overrode them with `HUBSPOT_PRIVATE_APP_TOKEN`, inferred from
> `docker image inspect` and the ENV-branch spec. The image accepts that name but resolves
> it **lazily via Secret Manager**, so nothing failed at startup — only on the first real
> write, four steps into an end-to-end test. The working configuration is:
> ```bash
> gcloud run services update lqabr-dev-mcp --region=us-central1 --project=ldqfingsrv-dev \
>   --update-env-vars="LQABR_SECRET_PROJECT=ldqfingsrv-dev,\
> LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN=projects/432617526728/secrets/lqabr-hubspot-access-token/versions/latest"
> ```
> **This is still a manual `gcloud run services update`, not a script.** It will be lost on a
> clean rebuild — MCP has no deploy script of its own. Open item.

#### Fix for #3 — industry normalisation

`summary_core/mcp/hubspot.py` gained `_normalise_industry()`, applied in **both** write
paths. It coerces case and separators only:

| Input | Output |
| --- | --- |
| `Healthcare` / `healthcare` | `HEALTHCARE` |
| `financial services` / `Financial-Services` | `FINANCIAL_SERVICES` |
| `Manufacturing` (not a configured option) | `MANUFACTURING` — passed through for the MCP to reject |

Deliberately **not** fuzzy-matched to the nearest option. The MCP's own error explains why:
*"a near-miss selects zero leads and raises no error"* — a wrong-but-accepted value is worse
than a rejection, and guessing a lead's industry is not this function's job.

The option list is configurable via `LQABR_SUMMARY_HUBSPOT_INDUSTRY_OPTIONS`
(`settings.hubspot_industry_options`, default `FINANCIAL_SERVICES, LEGAL_SERVICES,
HEALTHCARE`) so a portal change is config, not code.

Five existing tests asserted the old raw casing (`"Software"`); they were updated to the
normalised form. **`agents/summary` suite: 190 passed.**

### 1. End results

Six Cloud Run services: `lqabr-dev-{gtwy,mcp,summary,research,email,txtv}`.
`gtwy` public (`ingress=all`, `allow-unauthenticated`, app-level HMAC as the boundary);
the other five `ingress=internal --no-allow-unauthenticated`.

**Corrections that must be applied at deploy time:**

- **`--max-instances=1 --min-instances=1` on the gateway.** `agents/gateway/src/router.py:400`
  holds the dedupe store as a per-process `OrderedDict`;
  `infra/gcp/07_deploy_gateway.sh:46` pins max=1 for exactly this reason. The live service
  currently runs `maxScale=20`, which permits duplicate emails and duplicate outbound calls
  to real leads on any HubSpot retry, with nothing logged.
- ~~**Scoped VPC egress.**~~ **WITHDRAWN — disproved by the P8a stage-3 probe.**
  `--vpc-egress=all-traffic` does **not** break the private mesh; a call from inside the VPC
  to an `ingress=internal` service succeeds. Keep `all-traffic` on all services.
- **`LQABR_MCP_BASE_URL`** (not `LQABR_TXTV_MCP_BASE_URL`) for txtv — see P8.

### 2. Commands

*(Pending.)*

### 3. Validation

*(Pending.)*

---

## P10 — Cutover and cleanup

**Status: NOT YET EXECUTED**

### 1. End results

- `lqabr-dev-gtwy-pub` and `lqabr-dev-ldpf` retired or explicitly retained with a reason.
- HubSpot webhook target repointed from ngrok to the Cloud Run gateway.
- End-to-end verification through the private ring.

### 2. Commands

*(Pending.)*

### 3. Validation

*(Pending.)*
