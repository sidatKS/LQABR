# text_voice's own image — no longer built from the shared parametrized
# infra/gcp/cloud-run/Dockerfile (2026-08-07, user request: keep the
# Dockerfile co-located with the agent it builds). The other five agents
# (ingestion, lead_profile, email, scheduling, orchestrator) are untouched
# and still build from infra/gcp/cloud-run/Dockerfile via 05_deploy_agents.sh
# — this file is used by docker-compose.yml's local build/push path only for
# now; the Cloud Run deploy path (05_deploy_agents.sh + cloudbuild.yaml)
# still points at the shared Dockerfile until that's explicitly repointed too.
#
# Must still be built from the REPO ROOT as build context (`docker build -f
# agents/text_voice/Dockerfile .` / docker-compose.yml's `context: .`),
# because it needs packages/lqabr_core from outside this directory.
#
#   SERVICE_KIND "webhook" (default) -> uvicorn ${APP_MODULE} — the prod path
#                "agent"             -> adk api_server — dev/test surface only
#
# Runs in Google's managed serverless sandbox (Cloud Run / gVisor) when
# deployed — no privileged access, per-revision isolation.
FROM python:3.12-slim

ARG SERVICE_KIND=webhook
# Rev 5 retired webhook_app.py — the FastAPI app lives in tools.py.
ARG APP_MODULE=tools:app
ENV AGENT_DIR=text_voice \
    SERVICE_KIND=${SERVICE_KIND} \
    APP_MODULE=${APP_MODULE} \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Shared core first (better layer caching), then the agent itself.
COPY packages/lqabr_core packages/lqabr_core
RUN pip install --no-cache-dir ./packages/lqabr_core

COPY agents/text_voice agents/text_voice
RUN pip install --no-cache-dir -r agents/text_voice/requirements.txt
# google-adk lives in requirements-dev.txt (the prod webhook runs pure
# uvicorn/FastAPI and doesn't need it) — only pulled in for the
# SERVICE_KIND=agent dev/test image, which runs `adk api_server`.
RUN if [ "${SERVICE_KIND}" = "agent" ] && [ -f "agents/text_voice/requirements-dev.txt" ]; then \
      pip install --no-cache-dir -r agents/text_voice/requirements-dev.txt ; fi

COPY infra/gcp/cloud-run/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER nobody
ENTRYPOINT ["/entrypoint.sh"]
