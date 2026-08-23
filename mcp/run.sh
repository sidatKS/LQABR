#!/usr/bin/env bash
# Run the official HubSpot MCP container. All knobs come from scripts/dev/mcp.config
# (env vars override it). Native logging: the server's stdout is tee'd to the
# configured host log file from INSIDE the run, and still visible via `docker logs`.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
# shellcheck disable=SC1091
source "$(dirname "$0")/mcp.config"

# resolve the log file (relative -> repo root) into host dir + container path
case "$MCP_LOG_FILE" in
  /*) HOST_LOG="$MCP_LOG_FILE" ;;
  *)  HOST_LOG="$ROOT/$MCP_LOG_FILE" ;;
esac
LOG_DIR="$(dirname "$HOST_LOG")"
LOG_BASE="$(basename "$HOST_LOG")"
mkdir -p "$LOG_DIR" && chmod 777 "$LOG_DIR"

docker rm -f "$MCP_CONTAINER" >/dev/null 2>&1 || true
docker run -d -p "${MCP_HOST_PORT}:8080" \
  -v "$MCP_ADC_FILE:/gcp/adc.json:ro" \
  -v "$LOG_DIR:/logs" \
  -e GOOGLE_APPLICATION_CREDENTIALS=/gcp/adc.json \
  -e LQABR_SECRET_PROJECT="$MCP_SECRET_PROJECT" \
  -e LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN="$MCP_SECRET_NAME" \
  --name "$MCP_CONTAINER" "$MCP_IMAGE" \
  sh -c "python hubspot-crm-mcp-server/hubspot_crm_server.py 2>&1 | tee -a /logs/$LOG_BASE" >/dev/null
sleep 5
docker ps --filter "name=$MCP_CONTAINER" --format '>> up: {{.Names}}  {{.Ports}}  {{.Status}}'
echo ">> logging natively -> $MCP_LOG_FILE"
