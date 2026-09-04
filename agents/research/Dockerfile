# ============================================================
# agents/research — the Research Agent's OWN image.
#
# BUILD CONTEXT IS THIS FOLDER, not the repo root:
#
#     docker build -t lqabr-dev-research agents/research
#
# That is the standalone contract expressed as a build: the image
# physically cannot contain packages/lqabr_core or the repo-root mcp/
# folder, because neither is in the context. The HubSpot MCP is reached
# over the network at runtime, so it is a URL in the environment, never
# a layer in this image.
#
# THE TREE SITS AT /app/agents/research, NOT AT /app.
# packages/research_core/settings.py resolves the repo root as
# parents[4] of itself (settings.py -> research_core -> packages ->
# research -> agents -> ROOT). Copied to /app, that index does not
# exist and the module dies at import with `IndexError: 4` before
# uvicorn can bind a port — the container never becomes ready and the
# revision fails health checks. Found by running the image, not by
# reading it. Keep the depth, or change settings.py and its tests.
#
# Secrets come from Secret Manager by NAME at run time. .env is excluded
# by .dockerignore and must never enter a layer.
# ============================================================
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    PYTHONPATH=/app/agents/research/src:/app/agents/research/packages

WORKDIR /app/agents/research

# Dependencies first, so a code change does not reinstall the world.
COPY requirements.txt requirements-otel.txt ./
# requirements-otel.txt pins `-r requirements.txt`, so this installs both.
# It is NOT optional in the image: the CMD runs under opentelemetry-instrument,
# which ships in these packages. Tests still use plain requirements.txt.
RUN pip install --no-cache-dir -r requirements-otel.txt

COPY packages ./packages
COPY src ./src
COPY config ./config
COPY VERSION ./VERSION

# Non-root, because nothing here needs to be root. /app is owned by the
# runtime user so the daily log files under logs/ are writable.
RUN useradd --create-home --uid 1001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

# Cloud Run health checks hit /health; the app answers as soon as the
# process binds, without waiting for the MCP service to wake up.
CMD ["sh", "-c", "exec opentelemetry-instrument uvicorn service_app:app --host 0.0.0.0 --port ${PORT} --app-dir /app/agents/research/src"]
