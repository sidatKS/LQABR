# LQABR lead_profile — Container Image Spec

For review before build & push, per Platform Engineering call (03-Aug-2026).
Owner: Stephen Miller · Component: lead_profile agent

## 1. Image identity

| Item | Value |
|---|---|
| Image name | `lqabr-dev-lead-profile` (scheme: `<project>-dev-<component>`) |
| Version tag | `0.1.0` (matches `pyproject.toml`; bumped per release) |
| Moving tag | `latest` — always pushed alongside the version, so the newest image is pullable without a version lookup |
| Registry | Google **Artifact Registry** |
| Full path | `us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-lead-profile:<tag>` |

> Registry host, project, repo and version are overridable via env vars
> (`REGISTRY`, `PROJECT`, `REPO`, `VERSION`) — defaults are the values above.
> **To confirm with Yashwanth/Saroja:** the Artifact Registry repository name
> (`lqabr` assumed) and region (`us-central1` assumed).

## 2. What's inside

| Layer | Detail |
|---|---|
| Base | `python:3.12-slim` |
| Package manager | `uv` (copied from `ghcr.io/astral-sh/uv`), `uv sync --frozen` against the committed `uv.lock` — reproducible builds, no requirements.txt |
| Code | `agents/lead_profile/` (ADK agent, deterministic orchestrator) + `packages/lqabr_core/`: `leadgen.hubspot` (HubSpot tools), `leadgen.server` (MCP front door), `leadgen.secrets` (resolver), `obs` (observability) |
| Entrypoint | `lead-profile-agent` — one ADK agentic run: CSVs → lead profiles (in memory) → HubSpot upsert |
| Runtime user | non-root `lqabr` (uid 1001) |
| Data | `/data/incoming` (input CSVs) and `/data/errors` (error files) on a mounted volume — container filesystem stays ephemeral |
| Secrets | **No secrets in the image.** Resolved at runtime from Google Secret Manager (`LQABR_SECRET_BACKEND=gcp`) via the service account's `secretmanager.secretAccessor` role |
| Model/API keys | None required — default orchestrator is deterministic (zero model calls) |

## 3. Build & push (Docker Compose only — no CI/CD)

One-time auth on the build machine (service account with Artifact Registry writer role):

```bash
gcloud auth activate-service-account --key-file=<sa-key>.json   # or existing ADC
gcloud auth configure-docker us-central1-docker.pkg.dev
```

Then:

```bash
docker compose --profile tag-only build   # builds 0.1.0 and latest (identical layers)
docker compose --profile tag-only push    # pushes both tags to Artifact Registry
# pin a different version:
VERSION=0.1.1 docker compose --profile tag-only build
VERSION=0.1.1 docker compose --profile tag-only push
```

Verify:

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr --include-tags
```

## 4. Explicitly out of scope (per the call)

- Cloud Run deployment — done by Swaroop from this image once it's in the registry
- CI/CD pipeline, vulnerability scans — enabled later by Swaroop
- No `docker run` production usage from compose; compose here exists to **build, tag, and push**
