# Parametrized image for every LQABR service (ADK agent or webhook app).
# Built from the REPO ROOT by infra/gcp/05_deploy_agents.sh via cloudbuild.yaml,
# or by docker-compose.yml at the repo root (see docs/IMAGE_BUILD_SPEC.md):
#
#   AGENT_DIR    agents/<name> directory to bake in (e.g. email)
#   SERVICE_KIND "service" -> uvicorn src/service_app.py — the agent's OWN
#                             domain surface (POST /hubspot/campaign, the
#                             Mailgun route, /health AND /healthz). This is
#                             what the gateway calls, and what the email
#                             agent deploys as.
#                "agent"   -> adk api_server — the GENERIC ADK runner. Its
#                             contract is POST /run with an ADK envelope
#                             after a session handshake; a gateway domain
#                             call cannot reach it. Kept for agents without
#                             a domain surface yet.
#                "webhook" -> uvicorn src.webhook_app:app (provider events only)
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
# [gcp] pulls google-cloud-secret-manager. WITHOUT IT the Secret Manager code
# path cannot run at all — `lqabr_core.secrets` raises ImportError and an
# environment variable becomes the only thing able to resolve a credential.
# Cloud Run's --set-secrets injects Secret Manager values as env vars, so a
# deploy through 05_deploy_agents.sh works either way; this makes the direct
# API path (LQABR_SECRETS_SOURCE=secret_manager, and rotation without a
# redeploy) actually available.
RUN pip install --no-cache-dir "./packages/lqabr_core[gcp]"

# The central MCP at the project root. Loaded IN-PROCESS by the agents —
# `outreach.py` puts the repo root on sys.path and imports mcp.hubspot at
# module import time, so an image without this directory dies on ImportError
# before it can serve a request. It is not a separate service and starts no
# second process; it is a plain package that must ship inside the image.
COPY mcp mcp

COPY agents/${AGENT_DIR} agents/${AGENT_DIR}
RUN pip install --no-cache-dir -r agents/${AGENT_DIR}/requirements.txt

# Run state must be writable by the runtime user. The container runs as
# `nobody`, and everything COPYed above is root-owned 755 — so the code-tree
# default (agents/email/.runstate) is NOT writable and every campaign would
# fail at step 3 with a 503 before a single email went out. Verified against
# the real image layout, not assumed.
#
# This is the path runstate.default_dir() already prefers when it exists, so
# no configuration is required for the container to work; the deploy also
# sets LQABR_EMAIL_RUNSTATE_DIR explicitly so the value is visible in the
# service description rather than implied.
#
# NOTE it is still per-instance and does not survive scale-to-zero. Durable
# run state (Firestore/GCS) remains open — this only makes the container
# able to run at all.
RUN mkdir -p /var/lib/lqabr/email/runstate \
    && chown -R nobody /var/lib/lqabr \
    && chmod -R u=rwX /var/lib/lqabr

COPY infra/gcp/cloud-run/entrypoint.sh /entrypoint.sh
# Normalise line endings inside the image. The repo is checked out on
# Windows with core.autocrlf unset, so a CRLF shebang makes the kernel
# look for an interpreter literally named /bin/sh\r, which surfaces as
# 'exec /entrypoint.sh: no such file or directory'. Belt and braces
# alongside .gitattributes.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

USER nobody
ENTRYPOINT ["/entrypoint.sh"]
