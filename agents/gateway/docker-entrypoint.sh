#!/usr/bin/env bash
# =====================================================================
# Agent Gateway entrypoint — starts the agentgateway sidecar, then the
# gateway itself, in one container ("one process group, one deploy").
#
# Order matters: the sidecar comes up first so that dispatch has somewhere to
# send the first trigger. If the sidecar cannot start, the gateway still runs and
# says so loudly — routing a lead late is recoverable, not routing it at all is
# not. Note the gateway posts to whatever LQABR_*_AGENT_URL points at, so when
# the sidecar is down those vars must name the agents directly for dispatch to
# succeed; /readyz and the audit log will show the failures either way.
# =====================================================================
set -uo pipefail

PORT="${PORT:-8080}"
GATEWAY_DIR="/app/agents/gateway"
SIDECAR_CONFIG="${GATEWAY_DIR}/config/agentgateway.yaml"

log() { printf '{"stream":"system","event":"entrypoint","message":"%s"}\n' "$1"; }

sidecar_pid=""

start_sidecar() {
  if [[ "${AGENTGATEWAY_ENABLED:-1}" != "1" ]]; then
    log "agentgateway disabled by AGENTGATEWAY_ENABLED=0 — dispatch uses LQABR_*_AGENT_URL as configured"
    return
  fi
  if ! command -v agentgateway >/dev/null 2>&1; then
    log "agentgateway binary not present (D-02) — dispatch uses LQABR_*_AGENT_URL as configured"
    return
  fi
  log "starting agentgateway sidecar with ${SIDECAR_CONFIG}"
  agentgateway -f "${SIDECAR_CONFIG}" &
  sidecar_pid=$!
  # Give it a moment, then confirm it is actually alive rather than assuming.
  sleep 2
  if ! kill -0 "${sidecar_pid}" 2>/dev/null; then
    log "agentgateway exited during startup — dispatch uses LQABR_*_AGENT_URL as configured; if those name the sidecar, dispatches will fail and be audited"
    sidecar_pid=""
  fi
}

shutdown() {
  log "shutting down"
  [[ -n "${sidecar_pid}" ]] && kill "${sidecar_pid}" 2>/dev/null
  [[ -n "${gateway_pid:-}" ]] && kill "${gateway_pid}" 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap shutdown SIGTERM SIGINT

start_sidecar

log "starting agent gateway on :${PORT}"
cd "${GATEWAY_DIR}/src" || exit 1
# --proxy-headers/--forwarded-allow-ips: Cloud Run terminates TLS and its proxy
# is not 127.0.0.1, so without these uvicorn reports http:// and the HubSpot
# signature check compares against the wrong URI.
uvicorn server:app --host 0.0.0.0 --port "${PORT}" --no-access-log \
  --proxy-headers --forwarded-allow-ips="*" &
gateway_pid=$!

# The gateway is the reason this container exists: if it dies, the container
# dies, and the orchestrator restarts us. A dead sidecar alone is survivable.
wait "${gateway_pid}"
exit_code=$?
log "gateway exited with ${exit_code}"
[[ -n "${sidecar_pid}" ]] && kill "${sidecar_pid}" 2>/dev/null
exit "${exit_code}"
