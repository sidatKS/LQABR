# Agent Gateway — Image Build & Push Spec

**Saroja Nemmaluri · 3 August 2026 · for review before the compose file is written**

---

## 1. What gets built

The Agent Gateway (`agents/gateway`) packaged as a container image, using its existing
`agents/gateway/Dockerfile`.

## 2. Image name and tags

```
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-gtwy:0.1.0
us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-gtwy:latest
```

| Part | Value |
|---|---|
| Project | `lqabr` |
| Environment | `dev` |
| Component | `gtwy` |
| Version | `0.1.0` |
| Moving tag | `latest` |

Every build pushes both tags — the fixed version, and `latest` moved to point at it.

Version comes from `agents/gateway/config/config.yaml` (`gateway.version: 0.1.0`).

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
  gtwy:
    build:
      context: .
      dockerfile: agents/gateway/Dockerfile
      tags:
        - "us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-gtwy:0.1.0"
        - "us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-gtwy:latest"
    image: "us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-gtwy:0.1.0"
```

## 5. Build and push

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev

docker compose build gtwy
docker compose push gtwy
```

Verify:

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr \
  --include-tags --project ldqfingsrv-dev
```

Both `0.1.0` and `latest` should be listed. If only the version tag pushes, the
`latest` tag is pushed explicitly with `docker push`.

---

**For review:** confirm the name `lqabr-dev-gtwy` and the tag format above. On
approval, the compose file is created and the image pushed.
