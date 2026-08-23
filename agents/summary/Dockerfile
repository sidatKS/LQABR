# ============================================================
# agents/summary — the Summary Agent's OWN image.
#
# BUILD CONTEXT IS THIS FOLDER, not the repo root:
#
#     docker build -t lqabr-summary-agent agents/summary
#
# That is the standalone contract expressed as a build: the image
# physically cannot contain packages/lqabr_core or the repo-root mcp/
# folder, because neither is in the context. The HubSpot MCP is reached
# over the network at runtime (LQABR_SUMMARY_MCP_BASE_URL), so it is a
# URL in the environment, never a layer in this image.
#
# One image, one service, one port. Zero instances at rest.
# ============================================================
FROM python:3.11-slim AS base

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

# Non-root, because nothing here needs to be root.
RUN useradd --create-home --uid 1001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

# Cloud Run health checks hit /health; the app answers as soon as the
# process binds, without waiting for the MCP container to wake up.
CMD exec uvicorn service_app:app --host 0.0.0.0 --port ${PORT} --app-dir /app/src
