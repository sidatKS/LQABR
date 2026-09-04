# LQABR Research Agent — container image build & push spec

**Component:** `research` · **Service:** `lqabr-dev-research` · **As of:** 2026-08-26
**Project:** `ldqfingsrv-dev` · **Region:** `us-central1`

Follows the house pattern set by `agents/summary` — the agent owns its Dockerfile and its
`infra/` scripts, and the **build context is the agent folder, not the repo root.**

---

## 1. Registry

```
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr
```

Repository `lqabr` already exists (created 2026-07-21). Nothing to provision.

## 2. Image naming

```
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-research
```

Follows the **live platform convention** `lqabr-dev-<component>` — the same as
`lqabr-dev-mcp`, `lqabr-dev-gtwy`, `lqabr-dev-txtv`, `lqabr-dev-ldpf`, and
`lqabr-dev-summary` (which was realigned from `lqabr-summary-agent` on 2026-08-26,
before its first deploy, while a rename was still free).

## 3. Tags

Two tags on every build:

| Tag | Purpose |
| --- | --- |
| `0.1.0` (from `agents/research/VERSION`) | **what Cloud Run deploys against.** Immutable in intent — a revision must resolve to a known image |
| `latest` | human convenience pointer only |

**Cloud Run does not auto-pull `:latest`.** Deploying always names the version tag.

> **Known limitation.** Re-building the same `VERSION` moves both tags to a new digest and
> orphans the previous one. The old image is not destroyed — it becomes untagged — which is
> exactly how this repository accumulated 29 untagged images before the P2 cleanup. Note
> also that four live services were found running **untagged** digests, so "delete all
> untagged" is unsafe; see the runbook's P2. A `<version>-g<gitsha>` scheme was proposed and
> deferred.

## 4. Build

```bash
bash agents/research/infra/02_build_push.sh
```

which resolves to:

```bash
gcloud builds submit agents/research \
  --project ldqfingsrv-dev \
  --config agents/research/infra/cloudbuild.yaml \
  --substitutions "_IMAGE=<image>,_TAG=<VERSION>" \
  --service-account "projects/ldqfingsrv-dev/serviceAccounts/lqabr-build@ldqfingsrv-dev.iam.gserviceaccount.com"
```

> **`--service-account` is mandatory.** Cloud Build defaults to the **compute** SA, which
> holds **zero** roles in this project, and fails with
> `does not have storage.objects.get access to ... _cloudbuild`. The Google-managed
> `432617526728@cloudbuild.gserviceaccount.com` *does* hold `cloudbuild.builds.builder` but
> is rejected outright — *"provide a user-managed service account"*. Hence `lqabr-build`
> (runbook P3), which holds only `logging.logWriter` (project),
> `artifactregistry.writer` (repo-scoped) and `storage.objectViewer` (bucket-scoped).

## 5. Structure

### 5.1 Build context — the agent folder

```
agents/research/          <- THE CONTEXT
├── Dockerfile
├── VERSION               0.1.0
├── requirements.txt
├── config/               config.yaml — MCP tool names, model, search knobs
├── packages/
│   └── research_core/    the agent's own core (settings, mcp client, pipeline, obs)
├── src/                  service_app.py, agent.py, composer.py, pipeline.py, schema.py
├── tests/
└── infra/                cloudbuild.yaml, config.sh, 02_build_push.sh
```

**The context is deliberately the agent folder, not the repo root.** That is the standalone
contract expressed as a build: the image *physically cannot* contain
`packages/lqabr_core` or the repo-root `mcp/`, because neither is in the context.
`agents/research/tests/test_standalone.py` enforces the same rule in code
(`FORBIDDEN_ROOTS = {"lqabr_core", "summary_core", "mcp", "agents", "packages"}`).

> **Deliberately NOT built from `infra/gcp/cloud-run/Dockerfile`.** That shared image
> `pip install`s `lqabr_core` and copies `mcp/` in — both of which contradict the standalone
> design and add layers for no benefit.

### 5.2 Inside the image

```
/app
├── requirements.txt
├── VERSION
├── config/               <- shipped; settings.py resolves <root>/config/config.yaml
├── packages/research_core/
└── src/                  <- uvicorn --app-dir /app/src
```

`PYTHONPATH=/app/src:/app/packages`.

`research_core` is **not** pip-installed. `agents/research/src/service_app.py:30-35` inserts
`../packages` onto `sys.path` at import time, so it resolves regardless of working directory.

**Runtime user: `agent`, uid 1001** — created by the Dockerfile, owns `/app`. Every LQABR
image now runs as this single non-root identity (runbook P2c). A per-component uid
(`re_agent`, `e_agent`, …) was considered and rejected: containers do not share a kernel, so
it would add no isolation, and D2 keeps one service account anyway.

> **Path-depth trap, already fixed.** `research_core/settings.py` computed
> `_REPO_ROOT = Path(__file__).resolve().parents[4]`, valid only in the repo layout. The
> image flattens `agents/research` to `/app`, leaving two fewer levels, so `parents[4]`
> raised `IndexError` **at module import** — the container would die before binding the port
> and Cloud Run would report only its generic *"failed to start and listen on PORT=8080"*.
> Summary had the identical bug and failed exactly this way on 2026-08-26; both were fixed
> to fall back to the agent root.

### 5.3 What must NOT be in the image

| Item | Why |
| --- | --- |
| `packages/lqabr_core` | standalone contract; asserted at build time |
| repo-root `mcp/` | the MCP is a **network service**, reached at `LQABR_RESEARCH_MCP_BASE_URL` — never a layer |
| any credential | secrets arrive at runtime from Secret Manager |
| `.env` | git-ignored, local only |

## 6. Build-time assertions

`agents/research/infra/cloudbuild.yaml` fails the build — before any push — if:

1. **Runtime user is not `agent`.** Catches a silent regression to root or `nobody`, which
   would otherwise surface as a permission error at runtime.
2. **`lqabr_core` is importable inside the image.** Guards the standalone contract at the
   layer where it can actually be broken.
3. **The ID-token helper misbehaves.** `auth_header()` must return `{}` for loopback, strip
   the path when deriving a `run.app` audience, and return nothing for a vendor URL such as
   `api.hubapi.com` — a Google token must never be attached to a third-party call.

Nothing is pushed unless all three pass.

## 7. Runtime dependencies (not in the image)

| Dependency | How it arrives |
| --- | --- |
| HubSpot access | **only** via the MCP service — `LQABR_RESEARCH_MCP_BASE_URL` |
| Anthropic key | Secret Manager `lqabr-anthropic-api-key` |
| Auth to the private MCP | Google ID token minted at runtime from the metadata server |
| Egress to Anthropic | Cloud NAT, fixed IP `34.45.4.100` |

## 8. Commands

```bash
# build + push (build SA is wired into the script)
bash agents/research/infra/02_build_push.sh

# what landed
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr \
  --include-tags --format='table(package,tags,updateTime.date("%Y-%m-%d"))' | grep research

# local inspection before trusting a build
docker run --rm --entrypoint id \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-research:0.1.0
#   -> uid=1001(agent) gid=1001(agent)
```

## 9. Open items

- **No `03_deploy_run.sh` yet.** Research has `cloudbuild.yaml`, `config.sh` and
  `02_build_push.sh`; the deploy script still has to be written, mirroring summary's
  (VPC, `ingress=internal`, `gen2`, writable log volume).
- **Dependencies are unpinned** (`anthropic>=0.40`, `fastapi>=0.115`, …), so two builds of
  the same commit can produce different images. A lockfile was proposed and deferred.
- **This is not the only build path in the repo.** `infra/gcp/00-07`,
  `infra/dev/deploy/deploy.sh`, `infra/docker-compose.yml` and
  `agents/summary/infra/` all exist and disagree on names. Pick one and deprecate the rest.
