# ============================================================
# agents/research — the Research Agent's OWN image.
#
# BUILD CONTEXT IS THIS FOLDER, not the repo root:
#
#     docker build -t lqabr-dev-research agents/research
#
# That is the standalone contract expressed as a build, exactly as
# agents/summary/Dockerfile does it: the image physically CANNOT contain
# packages/lqabr_core or the repo-root mcp/ folder, because neither is in
# the context. research_core/tests/test_standalone.py enforces the same rule
# in code (FORBIDDEN_ROOTS = lqabr_core, summary_core, mcp, agents, packages);
# this file enforces it in the build.
#
# Deliberately NOT built from infra/gcp/cloud-run/Dockerfile — that shared
# image installs lqabr_core and copies mcp/ in, which would contradict the
# standalone design and bloat the layer set for no benefit.
#
# The HubSpot MCP is reached over the network at runtime
# (LQABR_RESEARCH_MCP_BASE_URL), so it is a URL in the environment, never a
# layer in this image.
#
# One image, one service, one port. Zero instances at rest.
# ============================================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    PYTHONPATH=/app/src:/app/packages

WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY packages ./packages
COPY src ./src
COPY VERSION ./VERSION

# The config map ships with the image. settings.py resolves it as
# <agent root>/config/config.yaml (parents[2] of the settings module), which
# is /app/config here. Env vars still override every value; without this the
# agent would silently fall back to code defaults for the MCP tool names.
COPY config ./config

# Non-root, uid 1001 / `agent` — the single runtime identity shared by every
# LQABR image (see agents/summary/Dockerfile and infra/gcp/cloud-run/Dockerfile).
RUN useradd --create-home --uid 1001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

# Cloud Run health checks hit /health; the app answers as soon as the process
# binds, without waiting for the MCP service to wake up.
CMD exec uvicorn service_app:app --host 0.0.0.0 --port ${PORT} --app-dir /app/src
