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
ENV AGENT_DIR=${AGENT_DIR} \
    SERVICE_KIND=${SERVICE_KIND} \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Shared core first (better layer caching), then the agent.
COPY packages/lqabr_core packages/lqabr_core
RUN pip install --no-cache-dir ./packages/lqabr_core

COPY agents/${AGENT_DIR} agents/${AGENT_DIR}
RUN pip install --no-cache-dir -r agents/${AGENT_DIR}/requirements.txt

# ADK writes per-agent session/artifact storage under the agent dir at runtime.
# The process runs as nobody (65534); make that path writable.
RUN mkdir -p "agents/${AGENT_DIR}/src/.adk" \
 && chown -R 65534:65534 "agents/${AGENT_DIR}"

COPY infra/gcp/cloud-run/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER nobody
ENTRYPOINT ["/entrypoint.sh"]
