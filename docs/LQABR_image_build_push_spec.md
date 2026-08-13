# LQABR — Container Image Build & Push Spec

**Deliverable requested on the 3 Aug call: the spec, for review, before the compose
file is written.**

| | |
|---|---|
| **Prepared by** | Saroja Nemmaluri, Yashwanth Bandaru (co-leads) |
| **For review by** | Swaroop Venkatagiri |
| **Date** | 3 August 2026 |
| **Status** | **DRAFT — awaiting review.** Part C lists what we need from you. |

---

## What was asked

> *"Simply your Docker compose should produce an image and push it to the Google
> registry. That's all I want."*
> *"Initially give me the spec… let's review it and then create the compose file."*

**In scope:** a Docker Compose file that builds each component into an image, tags it
correctly, and pushes it to the Google image registry.

**Out of scope, as directed:** Cloud Run deployment (*"Once I have it, I will use
Cloud Run to initiate"*), CI/CD pipelines (*"I did not ask you to run whole CI and
CD"*), and vulnerability scanning (to be enabled at the registry by Swaroop).

---

# PART A — The standard

Everything below is the contract all four owners build against. One convention, ten
images.

## A1. Registry

| | |
|---|---|
| Project | `ldqfingsrv-dev` |
| Region | `us-central1` |
| Repository | `lqabr` |
| **Base path** | `us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr` |

Already provisioned by `infra/gcp/00_enable_apis.sh`. This is **Artifact Registry** —
Google's current image registry. The older Container Registry (`gcr.io`) is deprecated.

## A2. Image name

```
<project> - <env> - <component>
  lqabr   -  dev  -   gtwy        →  lqabr-dev-gtwy
```

Per the call: *"It has to be the project name, dev, and then your four-letter
component."*

Registry names must be **lowercase** — an Artifact Registry rule, not a style choice.

## A3. Tags — two per build

| Tag | Purpose | Mutable? |
|---|---|---|
| `:0.1.0` | This exact build, forever | No |
| `:latest` | Moves to the newest build | Yes |

> *"When I pull the latest version it should have the versioning properly… it should
> have a latest tag in it at the end. Whatever the latest, I have to be able to pull
> it."*

**Full example — Agent Gateway v0.1.0:**

```
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-gtwy:0.1.0
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-gtwy:latest
```

## A4. The ten images

| # | Component | Source | Dockerfile | Image name |
|---|---|---|---|---|
| 1 | Agent Gateway | `agents/gateway` | own | `lqabr-dev-gtwy` |
| 2 | Ingestion agent | `agents/ingestion` | shared | `lqabr-dev-ings` |
| 3 | Lead Profile agent | `agents/lead_profile` | shared | `lqabr-dev-ldpf` |
| 4 | Email agent | `agents/email` | shared | `lqabr-dev-emal` |
| 5 | Text/Voice agent | `agents/text_voice` | shared | `lqabr-dev-txvc` |
| 6 | Scheduling agent | `agents/scheduling` | shared | `lqabr-dev-schd` |
| 7 | Orchestrator agent | `agents/orchestrator` | shared | `lqabr-dev-orch` |
| 8 | Email webhook | `agents/email` | shared | `lqabr-dev-emwh` |
| 9 | Text/Voice webhook | `agents/text_voice` | shared | `lqabr-dev-tvwh` |
| 10 | Scheduling webhook | `agents/scheduling` | shared | `lqabr-dev-scwh` |

- **shared** = `infra/gcp/cloud-run/Dockerfile`, parametrised by `AGENT_DIR` +
  `SERVICE_KIND` (`agent` \| `webhook`)
- **own** = `agents/gateway/Dockerfile` — the gateway is the one exception; it also
  installs the agentgateway binary and runs `docker-entrypoint.sh`

`ldpf` follows the abbreviation given on the call. The rest follow the same pattern.

## A5. Version source

Each component gets a **`VERSION` file** — one line, semver:

```
agents/gateway/VERSION   →   0.1.0
```

Read by Compose at build time. Nobody types a version by hand, so no image can be
mislabelled. The gateway's `0.1.0` already exists as `gateway.version` in
`config/config.yaml` and is stamped into every audit record — the file makes it
authoritative rather than duplicated.

Bump **before** the build that publishes it: patch = fix, minor = new behaviour,
major = breaking change.

---

# PART B — The flow

How an image gets from source to registry. Six stages.

```
   ┌──────────────────────────────────────────────────────────────┐
   │  SOURCE                                                      │
   │  agents/<component>/  +  packages/lqabr_core/  +  Dockerfile │
   └───────────────────────────┬──────────────────────────────────┘
                               │
       ┌───────────────────────▼───────────────────────┐
       │  STAGE 1 — DECLARE VERSION                    │
       │  agents/<component>/VERSION  →  0.1.0         │
       └───────────────────────┬───────────────────────┘
                               │
       ┌───────────────────────▼───────────────────────┐
       │  STAGE 2 — DEFINE SERVICE                     │
       │  docker-compose.yml: one block per component  │
       │  build context = repo root                    │
       │  tags = :<version> and :latest                │
       └───────────────────────┬───────────────────────┘
                               │
       ┌───────────────────────▼───────────────────────┐
       │  STAGE 3 — AUTHENTICATE                       │
       │  gcloud auth configure-docker                 │
       │    us-central1-docker.pkg.dev                 │
       └───────────────────────┬───────────────────────┘
                               │
       ┌───────────────────────▼───────────────────────┐
       │  STAGE 4 — BUILD                              │
       │  docker compose build <component>             │
       │  → image built locally, both tags applied     │
       └───────────────────────┬───────────────────────┘
                               │
       ┌───────────────────────▼───────────────────────┐
       │  STAGE 5 — PUSH                               │
       │  docker compose push <component>              │
       │  → both tags land in Artifact Registry        │
       └───────────────────────┬───────────────────────┘
                               │
       ┌───────────────────────▼───────────────────────┐
       │  STAGE 6 — VERIFY                             │
       │  gcloud artifacts docker images list          │
       │  → confirm version + latest are present       │
       └───────────────────────┬───────────────────────┘
                               │
   ┌───────────────────────────▼──────────────────────────────────┐
   │  OUTPUT — handed to Swaroop                                  │
   │  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/            │
   │      lqabr-dev-gtwy:0.1.0                                    │
   │      lqabr-dev-gtwy:latest              → Cloud Run from here│
   └──────────────────────────────────────────────────────────────┘
```

## Stage 1 — Declare the version

Create `VERSION` in each component directory, one line, e.g. `0.1.0`.
**Owner:** each component owner. **Output:** ten `VERSION` files.

## Stage 2 — Define the service

One `docker-compose.yml` at the **repo root**, one service block per component.
Compose is used here purely as a build-and-push tool — no ports, no networks, no
`depends_on`. The stack is not run from it.

Build context is the repo root for every service, because each image copies
`packages/lqabr_core` as well as its own directory.

```yaml
# docker-compose.yml (repo root) — build & push only
services:

  # --- 1. Agent Gateway: its own Dockerfile ---
  gtwy:
    build:
      context: .
      dockerfile: agents/gateway/Dockerfile
      tags:
        - "${REGISTRY}/lqabr-${ENV}-gtwy:${GTWY_VERSION}"
        - "${REGISTRY}/lqabr-${ENV}-gtwy:latest"
    image: "${REGISTRY}/lqabr-${ENV}-gtwy:${GTWY_VERSION}"

  # --- 3. Lead Profile agent: shared parametrised Dockerfile ---
  ldpf:
    build:
      context: .
      dockerfile: infra/gcp/cloud-run/Dockerfile
      args:
        AGENT_DIR: lead_profile
        SERVICE_KIND: agent
      tags:
        - "${REGISTRY}/lqabr-${ENV}-ldpf:${LDPF_VERSION}"
        - "${REGISTRY}/lqabr-${ENV}-ldpf:latest"
    image: "${REGISTRY}/lqabr-${ENV}-ldpf:${LDPF_VERSION}"

  # --- 8. Email webhook: same source dir, different SERVICE_KIND ---
  emwh:
    build:
      context: .
      dockerfile: infra/gcp/cloud-run/Dockerfile
      args:
        AGENT_DIR: email
        SERVICE_KIND: webhook
      tags:
        - "${REGISTRY}/lqabr-${ENV}-emwh:${EMWH_VERSION}"
        - "${REGISTRY}/lqabr-${ENV}-emwh:latest"
    image: "${REGISTRY}/lqabr-${ENV}-emwh:${EMWH_VERSION}"

  # …remaining seven follow the ldpf/emwh pattern
```

Build variables live in a root `.env` — no credentials, safe to commit:

```dotenv
REGISTRY=us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr
ENV=dev
GTWY_VERSION=0.1.0
LDPF_VERSION=0.1.0
EMWH_VERSION=0.1.0
```

> ⚠️ This is **not** `agents/gateway/config/.env`, which holds the HubSpot secret and
> is gitignored. Two different files. Renaming one is decision **D5**.

**Owner:** co-leads write the file; each owner supplies their block.
**Verified:** the `build.tags` syntax above parses correctly under Docker Compose
v5.1.3.

## Stage 3 — Authenticate

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
```

Once per machine. Uses existing `gcloud` credentials — no key file on anyone's laptop.

For automation only, a dedicated service account:

| | |
|---|---|
| Account | `lqabr-image-push@ldqfingsrv-dev.iam.gserviceaccount.com` |
| Role | `roles/artifactregistry.writer` (repository-scoped) |
| Login | `cat key.json \| docker login -u _json_key --password-stdin https://us-central1-docker.pkg.dev` |

Separate from the runtime SA (`lqabr-agent-dev`) so publish rights and runtime rights
are not the same credential. The key file must never be committed — decision **D4**.

## Stage 4 — Build

```bash
docker compose build gtwy        # one component
docker compose build             # all ten
```

Applies both tags in a single build. **Output:** image present locally with both tags.

## Stage 5 — Push

```bash
docker compose push gtwy
docker compose push
```

**One item to confirm on first run:** whether `docker compose push` sends *both* tags
or only the primary `image:` tag. This varies by Compose version and could not be
tested while writing this spec (no Docker daemon available). If only one tag lands,
the fallback is explicit and reliable:

```bash
docker push "${REGISTRY}/lqabr-dev-gtwy:latest"
```

Flagged rather than assumed, because "I have to be able to pull latest" was a stated
requirement. Will be confirmed on the first real build and this spec corrected.

## Stage 6 — Verify

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr \
  --include-tags --project ldqfingsrv-dev
```

Both tags must appear. **Output:** the registry paths reported back to Swaroop.

---

# PART C — Decisions needed from you

| # | Question | Our recommendation | Why |
|---|---|---|---|
| **D1** | Artifact Registry or `gcr.io`? | **Artifact Registry** | Already provisioned; `gcr.io` is deprecated |
| **D2** | Confirm the ten component codes (A4) | As tabled | `ldpf` matches the abbreviation you gave |
| **D3** | Webhooks: own codes (`emwh`) or suffix (`emal-hook`)? | **Own codes** | Keeps every name to the same three-part shape |
| **D4** | Push auth: SA key, or `gcloud auth configure-docker`? | **`configure-docker`** for people, key for automation | Avoids key files on laptops |
| **D5** | Root `.env` name, given the gateway already has one | **Rename** the build one | Two files called `.env` invites a secret leak |
| **D6** | Is `dev` in the image name, or only the tag? | **In the name** | Matches *"project name, dev, then component"* |

---

# PART D — Ownership and sequence

| Owner | Components |
|---|---|
| Saroja Nemmaluri (co-lead) | Agent Gateway — `gtwy` |
| Yashwanth Bandaru (co-lead) | *to confirm* |
| Stephen Miller | Lead Profile, Ingestion — `ldpf`, `ings` |
| Rao Duggineni | *to confirm* |

**On approval:**

1. Co-leads create the root `docker-compose.yml` and `.env`
2. Each owner adds a `VERSION` file and their service block
3. **`gtwy` is built and pushed first** as the reference implementation, confirming
   the Stage 5 two-tag behaviour
4. Remaining owners follow the proven pattern
5. Registry paths reported back — Cloud Run deployment proceeds from there

---

## Readiness note — Agent Gateway

Functionality is complete and proven end-to-end. On 31 July a real HubSpot webhook,
fired by a live CRM lead (contact `523828708059`, one of the 263 production records),
was signature-verified, routed by the registry rules, and handed to an agent in
**11.63 ms** — with a second event on the same property correctly discarded for not
matching the routing condition.

Full evidence: `docs/agent_gateway_hubspot_connection_log.md`.

The gateway is ready to be imaged.
