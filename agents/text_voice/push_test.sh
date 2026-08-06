#!/usr/bin/env bash
# End-to-end PUSH test for the text_voice webhook service — the production
# transport, run locally. Unlike the ADK `test <id>` command (which POLLS
# GET /call/{id}), this exercises the real design shape:
#
#   curl -> POST /voice_agent/lead -> pipeline dials -> call ends ->
#   Vapi PUSHES the report through a cloudflared tunnel -> POST /voice_agent/vapi_report
#   -> Steps 7/8 -> HubSpot
#
# The only piece of production this skips is the Agent Gateway (Vapi is
# pointed straight at this service's /voice_agent/vapi_report for the test).
#
# Usage:   ./push_test.sh [objectId]      # default: 524046551750 (rao.d)
# Stop:    Ctrl+C (cleans up uvicorn + tunnel)
#
# Needs: .env in this directory with LQABR_VAPI_API_KEY,
# LQABR_VAPI_PHONE_NUMBER_ID, LQABR_HUBSPOT_ACCESS_TOKEN, ANTHROPIC_API_KEY.

set -euo pipefail
cd "$(dirname "$0")"

OBJECT_ID="${1:-524046551750}"
PORT=8082

# ---- env ------------------------------------------------------------------
[ -f .env ] || { echo "ERROR: no .env in $(pwd)"; exit 1; }
set -a; source .env; set +a
: "${LQABR_VAPI_API_KEY:?LQABR_VAPI_API_KEY missing/empty in .env}"
: "${LQABR_HUBSPOT_ACCESS_TOKEN:?LQABR_HUBSPOT_ACCESS_TOKEN missing/empty in .env}"

# ---- reset the test contact to stage entry --------------------------------
echo "[1/5] Resetting contact ${OBJECT_ID} (voice_status=PENDING, prob=30)..."
curl -sf -X PATCH "https://api.hubapi.com/crm/v3/objects/contacts/${OBJECT_ID}" \
  -H "Authorization: Bearer ${LQABR_HUBSPOT_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"properties":{"lqabr_voice_status":"PENDING","probability":"30"}}' >/dev/null \
  || { echo "ERROR: HubSpot reset failed"; exit 1; }

# ---- tunnel ---------------------------------------------------------------
CLOUDFLARED="${HOME}/cloudflared"
if [ ! -x "$CLOUDFLARED" ]; then
  echo "[2/5] Downloading cloudflared..."
  wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -O "$CLOUDFLARED" && chmod +x "$CLOUDFLARED"
fi
TUNNEL_LOG="$(mktemp)"
echo "[2/5] Starting tunnel to localhost:${PORT}..."
"$CLOUDFLARED" tunnel --url "http://localhost:${PORT}" >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

TUNNEL_URL=""
for _ in $(seq 1 30); do
  TUNNEL_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
  [ -n "$TUNNEL_URL" ] && break
  sleep 1
done
[ -n "$TUNNEL_URL" ] || { echo "ERROR: tunnel URL never appeared (see $TUNNEL_LOG)"; kill "$TUNNEL_PID"; exit 1; }
echo "        tunnel: $TUNNEL_URL"

# Vapi must push the report to us through the tunnel (no gateway locally).
export LQABR_VAPI_REPORT_CALLBACK_URL="${TUNNEL_URL}/voice_agent/vapi_report"
# tools.py logs a config.suspect warning when GATEWAY_BASE_URL is localhost;
# point it at the tunnel so the startup log reflects the real topology.
export LQABR_GATEWAY_BASE_URL="$TUNNEL_URL"

cleanup() {
  echo; echo "Stopping server and tunnel..."
  kill "${UVICORN_PID:-0}" "${TUNNEL_PID:-0}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---- the real webhook service ---------------------------------------------
echo "[3/5] Starting uvicorn (the production app) on port ${PORT}..."
( cd src && exec python -m uvicorn tools:app --port "$PORT" ) &
UVICORN_PID=$!
for _ in $(seq 1 20); do
  curl -sf "http://localhost:${PORT}/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -sf "http://localhost:${PORT}/healthz" >/dev/null \
  || { echo "ERROR: service never became healthy"; exit 1; }

# ---- fire the lead exactly the way the gateway would ----------------------
# 2026-08-05: corrected against a real captured Agent Gateway request — the
# gateway sends `object_id` (snake_case), not `objectId`. See _extract_object_id()
# in tools.py for the full envelope shape this now matches.
echo "[4/5] POST /voice_agent/lead {\"object_id\": \"${OBJECT_ID}\"} ..."
curl -s -X POST "http://localhost:${PORT}/voice_agent/lead" \
  -H "Content-Type: application/json" \
  -d "{\"object_id\": \"${OBJECT_ID}\"}"
echo

echo "[5/5] Phone should ring shortly. Answer it (or let it hit voicemail)."
echo "      When the call ends, watch below for Vapi's PUSH arriving at"
echo "      POST /voice_agent/vapi_report, then step7/step8 logs. Ctrl+C when done."
wait "$UVICORN_PID"
