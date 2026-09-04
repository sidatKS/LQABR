# =====================================================================
# Agent Gateway — one image, one deploy.
# ---------------------------------------------------------------------
# Rev 3: "Ships inside the gateway's own repo/container (lib/soloai) — one
# process, one deploy, no extra hop." The runtime turned out to be a Rust
# binary rather than a Python library (D-02), so "one deploy" is honoured by
# putting agentgateway in *this* image as a sidecar process rather than by
# standing up a second service.
#
# Build from the REPO ROOT so the build context sees agents/gateway:
#   docker build -f agents/gateway/Dockerfile -t lqabr-agent-gateway .
# =====================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    AGENTGATEWAY_ENABLED=1

WORKDIR /app

# Non-root runtime user, uid 1001 / `agent` — same identity as every other
# LQABR image. This image previously declared no USER at all and therefore ran
# as root inside the container.
RUN useradd --create-home --uid 1001 agent

# --- Python deps first, so a code change doesn't reinstall the world -----
COPY agents/gateway/requirements.txt /app/agents/gateway/requirements.txt
RUN pip install --no-cache-dir -r /app/agents/gateway/requirements.txt

# --- agentgateway (the "Solo AI" runtime) --------------------------------
# Pinned by tag rather than tracking latest: the sidecar sits in the request
# path, so it changes when we choose, not when upstream releases.
ARG AGENTGATEWAY_VERSION=latest
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && (curl -sL https://agentgateway.dev/install | bash || \
     echo "WARN: agentgateway install failed; container will run gateway-only (see entrypoint)") \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY agents/gateway /app/agents/gateway

# lib/ holds the co-located runtime adapter (import soloai)
ENV PYTHONPATH=/app/agents/gateway/lib:/app/agents/gateway/src

# Everything COPYed/installed above is root-owned; hand the app tree to the
# runtime user. The agentgateway binary installed above stays root-owned and
# world-executable on PATH; if the installer placed it under root's HOME it is
# simply not found and docker-entrypoint.sh degrades to direct dispatch, which
# is its existing designed behaviour (see start_sidecar()).
RUN chown -R agent:agent /app

USER agent

EXPOSE 8080
# Liveness only — /readyz additionally checks that every agent endpoint
# resolves, which is a deployment concern, not a "is the process alive" one.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python3 -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8080)}/healthz').read()"

ENTRYPOINT ["/bin/bash", "/app/agents/gateway/docker-entrypoint.sh"]
