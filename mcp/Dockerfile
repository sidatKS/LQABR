# =====================================================================
# HubSpot MCP — always-on local container for the LQABR MVP.
# Serves mcp/http_server.py (streamable-HTTP JSON-RPC) on :8080.
# BUILD CONTEXT = repo root (needs packages/lqabr_core AND mcp/):
#   docker build -f mcp/Dockerfile -t lqabr-hubspot-mcp:local .
# =====================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1) transport deps (cached unless requirements change)
COPY mcp/requirements.txt /app/mcp/requirements.txt
RUN pip install --no-cache-dir -r /app/mcp/requirements.txt

# 2) the shared library the tools use (env-only secrets -> no GCP libs needed)
COPY packages/lqabr_core /app/packages/lqabr_core
RUN pip install --no-cache-dir ./packages/lqabr_core

# 3) the MCP package (local `mcp` — NOT the PyPI MCP SDK; no collision because
#    only mcp.hubspot + mcp.http_server are imported)
COPY mcp /app/mcp

# env-mode secrets by default; the token is injected at run time via --env-file
ENV LQABR_SECRETS_SOURCE=env \
    LQABR_MCP_PATH=/mcp

EXPOSE 8080
CMD ["uvicorn", "mcp.http_server:app", "--host", "0.0.0.0", "--port", "8080"]
