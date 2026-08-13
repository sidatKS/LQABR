# Parametrized image for every LQABR service (ADK agent or webhook app).
# Built from the REPO ROOT by infra/gcp/05_deploy_agents.sh via cloudbuild.yaml:
#
#   AGENT_DIR    agents/<name> directory to bake in (e.g. email)
#   SERVICE_KIND "agent"   -> adk api_server (A2A-capable agent endpoint)
#                "webhook" -> uvicorn src.webhook_app:app
#
# The container runs in Google's managed serverless sandbox (Cloud Run /
# gVisor) — no privileged access, per-revision isolation.
FROM python:3.12-slim

ARG AGENT_DIR
ARG SERVICE_KIND=agent
# Which module exposes the FastAPI `app` for webhook-kind images. Baked into
# the image so a bare `gcloud run deploy` (no env vars) boots correctly —
# text_voice builds with APP_MODULE=tools:app (webhook_app.py retired in
# Rev 5); default stays webhook_app:app for email/scheduling. Runtime env
# still overrides (entrypoint reads it), this is just the correct default.
ARG APP_MODULE=webhook_app:app
ENV AGENT_DIR=${AGENT_DIR} \
    SERVICE_KIND=${SERVICE_KIND} \
    APP_MODULE=${APP_MODULE} \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Shared core first (better layer caching), then the agent.
COPY packages/lqabr_core packages/lqabr_core
RUN pip install --no-cache-dir ./packages/lqabr_core

COPY agents/${AGENT_DIR} agents/${AGENT_DIR}
RUN pip install --no-cache-dir -r agents/${AGENT_DIR}/requirements.txt
# Agents that split dev deps out (text_voice: google-adk lives in
# requirements-dev.txt because its production webhook runs pure
# uvicorn/FastAPI) still need them for the SERVICE_KIND=agent image, which
# runs `adk api_server`. No-op for agents without the file and for
# webhook-kind builds.
RUN if [ "${SERVICE_KIND}" = "agent" ] && [ -f "agents/${AGENT_DIR}/requirements-dev.txt" ]; then \
      pip install --no-cache-dir -r agents/${AGENT_DIR}/requirements-dev.txt ; fi

COPY infra/gcp/cloud-run/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER nobody
ENTRYPOINT ["/entrypoint.sh"]
