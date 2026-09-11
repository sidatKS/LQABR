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
#   SERVICE_KIND is always "webhook" here -> uvicorn ${APP_MODULE}.
#   The "agent" (adk api_server) kind was dropped on 2026-08-18 along with ADK
#   itself: this agent has no `root_agent` any more. Everything reaches it over
#   plain HTTP — the orchestrator's A2A JSON-RPC `message/send` envelope is
#   parsed by tools.py's /voice_agent/lead directly (tools.py:621).
#
#   WARNING: infra/gcp/cloud-run/entrypoint.sh's `webhook` branch hardcodes
#   `uvicorn webhook_app:app` and IGNORES ${APP_MODULE}. webhook_app.py is
#   retired, so that shared entrypoint cannot serve this image. See
#   claude/deleted-files-audit-2026-08-18.md before deploying.
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

# Non-root runtime user, uid 1001 / `agent` — same identity as every other
# LQABR image (see infra/gcp/cloud-run/Dockerfile and agents/summary/Dockerfile).
RUN useradd --create-home --uid 1001 agent

# Shared core first (better layer caching), then the agent itself.
COPY packages/lqabr_core packages/lqabr_core
RUN pip install --no-cache-dir ./packages/lqabr_core

COPY agents/text_voice agents/text_voice
RUN pip install --no-cache-dir -r agents/text_voice/requirements.txt
# No conditional google-adk install any more — requirements-dev.txt is gone
# (2026-08-18, ADK removal). requirements.txt is the whole dependency set.

COPY infra/gcp/cloud-run/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Everything COPYed above is root-owned; hand the app tree to the runtime user.
RUN chown -R agent:agent /app

USER agent
ENTRYPOINT ["/entrypoint.sh"]
