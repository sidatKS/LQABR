# LQABR — container image build & push spec

> **REVISED 2026-08-04 — the email component now ships ONE image.**
> Platform Engineering (Swaroop, 12:45): *"why do you guys create the webhook
> image? What was the need for it?"* and (15:08) *"your mail gun has to send
> back the event triggers back to the SAME agent."*
>
> `lqabr-dev-email-webhook` is **gone**, `agents/email/src/webhook_app.py` is
> deleted, and `email-agent` is built with `SERVICE_KIND=service` — the
> agent's own FastAPI surface serving the gateway entry AND the Mailgun push.
> The image NAME is unchanged, so the Cloud Run URL is unchanged.
> Rows below that pair an `-agent` with an `-webhook` image still apply to
> text_voice and scheduling, not to email.

**Status: built and verified locally. NOTHING PUSHED — awaiting Swaroop's approval.**

Requested on the Platform Engineering call, 2026-08-03. Prepared by Yashwanth Bandaru
& Saroja Nemmaluri (asked to lead, supporting Stephen Miller and Rao Duggineni).
Naming follows the email-agent spec submitted for review.

**Scope, as stated on the call:** produce an image per service with Docker Compose and
push it to the Google image registry, correctly tagged and versioned, with a `latest`
that can always be pulled. **Explicitly out of scope:** CI/CD pipelines, vulnerability
scanning (Swaroop enables that later), and Cloud Run deployment — he takes the image
from the registry and drives Cloud Run himself.

---

## 1. Registry

| | |
|---|---|
| Type | Google Artifact Registry (Docker format) |
| Host | `us-central1-docker.pkg.dev` |
| Project | `ldqfingsrv-dev` |
| Repository | `lqabr` |
| Full base | `us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr` |

Already defined as `IMAGE_BASE` in `infra/gcp/config.dev.sh`; the compose file defaults
to the same value, so dev needs no configuration. Prod is one override away
(`IMAGE_BASE=…/ldqfingsrv/lqabr IMAGE_PREFIX=lqabr-prod`).

> ⚠️ `infra/gcp/config.sh` (prod) still carries `PROJECT_ID="your-gcp-project-id"`.

## 2. Image naming

```
<IMAGE_BASE>/<project>-<env>-<component>-<kind>:<version>
                └lqabr┘  └dev┘  └component┘  └agent|webhook┘
```

`<kind>` is required because three of the six agents ship **two** images from the same
source — historically an ADK `api_server` and a FastAPI webhook receiver — which would otherwise
collide. It also matches the Cloud Run service names already used by
`05_deploy_agents.sh` (`lqabr-<agent>-<kind>`), with the environment inserted.

| Service | Component | Agent image | Webhook image |
|---|---|---|---|
| email | `email` | `lqabr-dev-email-agent` (`SERVICE_KIND=service`) | — *(removed 2026-08-04)* |
| ingestion | `ingestion` | `lqabr-dev-ingestion-agent` | — |
| lead_profile | `lead-profile` | `lqabr-dev-lead-profile-agent` | — |
| text_voice | `text-voice` | `lqabr-dev-text-voice-agent` | `lqabr-dev-text-voice-webhook` |
| scheduling | `scheduling` | `lqabr-dev-scheduling-agent` | `lqabr-dev-scheduling-webhook` |
| orchestrator | `orchestrator` | `lqabr-dev-orchestrator-agent` | — |

Nine images. The email agent's two are the ones ready to publish now; the other seven
build from the same file when their owners are ready.

## 3. Tags

Every push publishes **two** tags on the same digest:

| Tag | Purpose |
|---|---|
| `:<version>` | Immutable. Semantic version, `MAJOR.MINOR.PATCH`, starting `0.1.0`. Never overwritten. |
| `:latest` | Moves to the most recent push, so `docker pull …:latest` always works without looking up a version. |

```
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-email-agent:0.1.0
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-email-agent:latest
```

Version is supplied at build time and defaults to `0.1.0`:
`VERSION=0.2.0 bash infra/gcp/compose_build_push.sh`

## 4. Build

One Dockerfile serves all nine images — `infra/gcp/cloud-run/Dockerfile`, already
parameterised:

| Build arg | Values | Effect |
|---|---|---|
| `AGENT_DIR` | `ingestion` · `lead_profile` · `email` · `text_voice` · `scheduling` · `orchestrator` | which `agents/<dir>` is baked in |
| `SERVICE_KIND` | `service` · `agent` · `webhook` | entrypoint: `uvicorn service_app:app` (email) · `adk api_server` · `uvicorn webhook_app:app` |

Base `python:3.12-slim`, runs as `nobody`, listens on `$PORT` (8080). Build context is
the repo root — the image needs `packages/lqabr_core`, `mcp/` and `agents/<dir>` together.

---

## 5. Structure

### 5.1 Repository — the build context

```
LQABR/                                   ← build context (repo root)
│
├── docker-compose.yml               NEW  9 build services, names + tags
├── .dockerignore                    NEW  keeps .venv/.git/.env out of the context
│
├── infra/gcp/
│   ├── compose_build_push.sh        NEW  build; push only when PUSH=1
│   ├── config.dev.sh                     IMAGE_BASE · AR_REPO · service lists
│   ├── config.sh                         prod equivalent (PROJECT_ID unset)
│   ├── 05_deploy_agents.sh               Cloud Run deploy — NOT part of this task
│   └── cloud-run/
│       ├── Dockerfile               CHG  + COPY mcp mcp
│       ├── entrypoint.sh                 service | agent | webhook switch
│       └── cloudbuild.yaml               used by 05_…; unchanged
│
├── docs/IMAGE_BUILD_SPEC.md         NEW  this document
│
├── packages/lqabr_core/                  shared library, pip-installed first
├── mcp/  hubspot/ auth · crm · schema · server     central MCP, in-process
│
└── agents/
    ├── ingestion/      → ingestion-agent
    ├── lead_profile/   → lead-profile-agent
    ├── email/          → email-agent  (one image, SERVICE_KIND=service)
    │   ├── requirements.txt              installed inside the image
    │   ├── skills/                       15 industry SKILL.md + DRAFTING_RULES.md
    │   │                                 read at RUNTIME — must ship
    │   └── src/  service_app · outreach · events · enums · runstate · observability
    ├── text_voice/     → text-voice-agent  +  text-voice-webhook
    ├── scheduling/     → scheduling-agent  +  scheduling-webhook
    └── orchestrator/   → orchestrator-agent
```

### 5.2 Inside the image

```
/app/
├── packages/lqabr_core/      also pip-installed into site-packages
├── mcp/                      importable from /app (repo root on sys.path)
│   └── hubspot/
└── agents/<AGENT_DIR>/
    ├── skills/               email only — read at runtime
    └── src/
/entrypoint.sh                SERVICE_KIND switch
```

`WORKDIR /app` · `USER nobody` · `PORT=8080` · no privileged access.

### 5.3 Registry after an approved push

```
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/
├── lqabr-dev-email-agent            :0.1.0  :latest     ← ready now
│   (lqabr-dev-email-webhook REMOVED 2026-08-04 — one image now)
├── lqabr-dev-ingestion-agent        :0.1.0  :latest
├── lqabr-dev-lead-profile-agent     :0.1.0  :latest
├── lqabr-dev-text-voice-agent       :0.1.0  :latest
├── lqabr-dev-text-voice-webhook     :0.1.0  :latest
├── lqabr-dev-scheduling-agent       :0.1.0  :latest
├── lqabr-dev-scheduling-webhook     :0.1.0  :latest
└── lqabr-dev-orchestrator-agent     :0.1.0  :latest
```

### 5.4 Build flow

```
docker-compose.yml
      │  service: email-agent
      │  args: AGENT_DIR=email, SERVICE_KIND=service
      ▼
infra/gcp/cloud-run/Dockerfile          context = repo root, minus .dockerignore
      │  COPY packages/lqabr_core → pip install
      │  COPY mcp                                    ← the fix in §6
      │  COPY agents/email        → pip install -r requirements.txt
      │  COPY entrypoint.sh
      ▼
image  lqabr-dev-email-agent   tagged :0.1.0 and :latest
      ▼
docker push  ×2 tags  →  Artifact Registry        ← REQUIRES PUSH=1 + approval
      ▼
(separately, Swaroop) gcloud run deploy --image …:latest
```

## 6. Two fixes this work required

**`COPY mcp mcp` added to the Dockerfile.** It copied `packages/lqabr_core` and
`agents/<dir>` but never `mcp/`. The agents put the repo root on `sys.path` and import
`mcp.hubspot` at module import time, so every HubSpot-touching container would have died
on `ImportError` before serving a request. **Affects all four of us** — the central MCP
landed after the Dockerfile was written.

**`.dockerignore` added.** The context is the repo root and there was no ignore file, so
every build uploaded `.venv` (hundreds of MB, rebuilt inside the image anyway) and
`.git`. More seriously, `agents/*/.env` holds live API keys, which must never reach a
layer that ships to a registry. Markdown is deliberately *not* excluded — the email
agent's skills are `.md` files read at runtime.

## 7. Verification performed

Build context contents were confirmed by materialising the context into a scratch image
and listing it — **112 files**:

| Check | Result |
|---|---|
| `.env` / `.venv` / `.git` / `__pycache__` excluded | ✅ none present |
| `tests/`, `docs/`, `conftest.py`, `pytest.ini` excluded | ✅ none present |
| all 15 `skills/*/SKILL.md` + `DRAFTING_RULES.md` present | ✅ 16 markdown files |
| `mcp/hubspot/server.py` present | ✅ |
| `packages/lqabr_core/lqabr_core/secrets.py` present | ✅ |
| `infra/gcp/cloud-run/entrypoint.sh` present | ✅ |
| compose renders all 9 image names correctly | ✅ `docker compose config --images` |
| `VERSION` / `IMAGE_PREFIX` / `IMAGE_BASE` overrides | ✅ |

**Not yet verified:** the layers above `FROM python:3.12-slim` — the pip installs and a
container start. Those need one `docker compose build email-agent` on a machine that can
reach Docker Hub. Everything above that line is confirmed.

## 8. Commands

```bash
# once per machine
gcloud auth login
gcloud auth configure-docker us-central1-docker.pkg.dev

# repository, if it does not exist yet
gcloud artifacts repositories create lqabr \
  --repository-format=docker --location=us-central1 --project=ldqfingsrv-dev

# BUILD ONLY — safe, nothing leaves the machine
bash infra/gcp/compose_build_push.sh email-agent

# inspect before approving
docker run --rm -it --entrypoint sh \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-email-agent:0.1.0
#   ls /app/mcp/hubspot /app/agents/email/skills
#   python -c "import sys; sys.path.insert(0,'/app'); import mcp.hubspot.server"

# PUSH — only after Swaroop approves this spec
PUSH=1 bash infra/gcp/compose_build_push.sh email-agent

# verify
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr \
  --include-tags --project ldqfingsrv-dev
```

The script **builds by default and refuses to push** without `PUSH=1`; a plain run prints
the tags it would have published and exits.

## 9. Permissions to push

On project `ldqfingsrv-dev`, for the pushing identity:

| Role | Why |
|---|---|
| `roles/artifactregistry.writer` | push images (`reader` alone cannot) |
| `roles/artifactregistry.repoAdmin` | only if it must also *create* the `lqabr` repo |

`artifactregistry.googleapis.com` must be enabled — `00_enable_apis.sh` covers it. The
Cloud Run runtime SA (`lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com`) needs
only `roles/artifactregistry.reader`, to pull at deploy time.

## 10. For Swaroop to confirm

1. Image names `lqabr-dev-email-agent` / `lqabr-dev-email-webhook` and the
   `<project>-<env>-<component>-<kind>` pattern.
2. Version baseline `0.1.0`.
3. Email agent's two images first, or all nine at once.
4. Then: approve the push.

---

*Task 2 — hosting these images on Cloud Run's serverless sandbox — is deliberately not
covered here. It is a separate discussion, to be held after the images exist.*
