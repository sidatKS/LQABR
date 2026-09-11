#!/usr/bin/env bash
# ============================================================
# Single source of truth for the HubSpot MCP service.
# Everything here is verified against docs/CloudRun_RunBook.md
# (P8a + P9, 2026-08-26). Source it before any script in this dir.
# ============================================================
export PROJECT_ID="ldqfingsrv-dev"
export PROJECT_NUMBER="432617526728"
export REGION="us-central1"

export MCP_SERVICE="lqabr-dev-mcp"
export MCP_SA="lqabr-agent-dev@${PROJECT_ID}.iam.gserviceaccount.com"

# Upstream: the image is PROMOTED from Docker Hub, not built from mcp/Dockerfile.
export MCP_UPSTREAM="tne736/lqabr-mcp-server:latest"
export MCP_AR="${REGION}-docker.pkg.dev/${PROJECT_ID}/lqabr/lqabr-dev-mcp"
export MCP_TAG="0.1.0"
# Provenance pin — upstream :latest is mutable, so this records what was actually promoted.
# 2026-08-28: upstream moved (6 new layers). Image CONTRACT verified unchanged before
# promoting: MCP_TRANSPORT/MCP_HOST/PORT=8080/MCP_PATH=/mcp/HUBSPOT_AUTH_MODE=private_app,
# USER=mcp, CMD=python hubspot-crm-mcp-server/hubspot_crm_server.py.
export MCP_DIGEST="sha256:aa91c5b8a567428e6da5e6cdd75d4a28f11c5d80b01ae029d2fa5f4c384b0c26"
# ^ that is the multi-arch INDEX digest (what the tag points at). Cloud Run resolves it
# to the linux/amd64 child manifest below, which is what revision 00005-ddf actually runs.
export MCP_PLATFORM_DIGEST="sha256:41d927b047ab9206e5b1489937d29fa8e1484844ac27a4413061aaac453c5856"
# Previous promotion, still untagged in AR — deploy this digest to roll back.
export MCP_PREV_DIGEST="sha256:1d5c84c7651808a769e78031e40c03e1c8e3dc519b9829d7cfad92de4603aeef"
# Immutable, never-reused tag written alongside 0.1.0 by 00_promote_image.sh.
export MCP_DATED_TAG="0.1.0-$(date -u +%Y%m%d)"

# Network — without these the service cannot reach or be reached by the private mesh.
export VPC_NETWORK="lqabr-vpc"
export VPC_SUBNET="lqabr-run-uscentral1"
export VPC_EGRESS="all-traffic"   # PROVEN safe for ingress=internal (P8a stage 3)

# Token contract. The image resolves the token LAZILY through its own LQABR_SECRET_*
# layer (HUBSPOT_AUTH_MODE=private_app is baked in). Do NOT use
# --set-secrets=HUBSPOT_PRIVATE_APP_TOKEN=... : the image accepts the name, starts
# cleanly, then fails on the first real write with SecretConfigError.
export MCP_SECRET_REF="projects/${PROJECT_NUMBER}/secrets/lqabr-hubspot-access-token/versions/latest"

echo "mcp config: ${MCP_SERVICE} @ ${REGION}/${PROJECT_ID}"
