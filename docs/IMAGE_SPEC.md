# Lead Profile Agent — Image Build & Push Spec

**Stephen Miller · 3 August 2026 · for review before the compose file is written**

---

## 1. What gets built

The lead_profile agent (`agents/lead_profile`) packaged as a container image,
using the repo's shared parametrized `infra/gcp/cloud-run/Dockerfile`
(python:3.12-slim, non-root, `AGENT_DIR=lead_profile`, `SERVICE_KIND=agent`):
`packages/lqabr_core` is installed first, then the agent's `requirements.txt`,
and the entrypoint serves the agent with `adk api_server agents/lead_profile/src`
(A2A/HTTP — the same deterministic CSVs → lead profiles → HubSpot run,
zero model calls by default).

## 2. Image name and tags

```
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-ldpf:0.1.0
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-ldpf:latest
```

| Part | Value |
|---|---|
| Project | `lqabr` |
| Environment | `dev` |
| Component | `ldpf` |
| Version | `0.1.0` |
| Moving tag | `latest` |

Every build pushes both tags — the fixed version, and `latest` moved to point at it.

Version comes from `packages/lqabr_core/pyproject.toml` (`version = "0.1.0"`).

## 3. Where it goes

| | |
|---|---|
| Registry | Artifact Registry |
| Project | `ldqfingsrv-dev` |
| Region | `us-central1` |
| Repository | `lqabr` |

## 4. The compose file

`docker-compose.yml` at the repo root:

```yaml
services:
  ldpf:
    build:
      context: .
      dockerfile: infra/gcp/cloud-run/Dockerfile
      args:
        AGENT_DIR: lead_profile
        SERVICE_KIND: agent
      tags:
        - "us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-ldpf:0.1.0"
        - "us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-ldpf:latest"
    image: "us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-ldpf:0.1.0"
```

Same `_AGENT`/`_KIND` parameters Cloud Build uses (`infra/gcp/cloud-run/cloudbuild.yaml`)
— one Dockerfile for every build path.

## 5. Build and push

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev

docker compose build ldpf
docker compose push ldpf
```

Verify:

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr \
  --include-tags --project ldqfingsrv-dev
```

Both `0.1.0` and `latest` should be listed. If only the version tag pushes, the
`latest` tag is pushed explicitly with `docker push`.

## 6. Notes

- No secrets in the image — HubSpot token is resolved at runtime from Google
  Secret Manager (`LQABR_SECRET_BACKEND=gcp`). No model/API key needed: the
  default orchestrator is deterministic.
- Cloud Run deployment and CI/CD (scans, pipelines) are out of scope here —
  handled by Swaroop from the pushed image.

---

**For review:** confirm the name `lqabr-dev-ldpf` and the tag format above. On
approval, the image is built and pushed.
