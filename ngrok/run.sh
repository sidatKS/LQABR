#!/usr/bin/env bash
# Open the static ngrok tunnel to the gateway. All knobs live in ngrok/ngrok.config
# (env vars override). ngrok writes its OWN log to the configured file (--log).
#
#   bash ngrok/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root
ROOT="$(pwd)"
HERE="$ROOT/ngrok"

# shellcheck disable=SC1091
source "$HERE/ngrok.config"

# authtoken from ngrok/.env (git-ignored); registered once, idempotent
if [[ -f "$HERE/.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/.env"
  if [[ -n "${NGROK_AUTHTOKEN:-}" ]]; then
    ngrok config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null 2>&1 || true
  fi
fi

case "$NGROK_LOG" in /*) LOG="$NGROK_LOG" ;; *) LOG="$ROOT/$NGROK_LOG" ;; esac
mkdir -p "$(dirname "$LOG")"

pkill -f "ngrok http $NGROK_PORT" 2>/dev/null || true
sleep 1
nohup ngrok http "$NGROK_PORT" --domain="$NGROK_DOMAIN" \
      --log="$LOG" --log-format=logfmt >/dev/null 2>&1 &
sleep 4
echo ">> tunnel: https://$NGROK_DOMAIN  ->  http://127.0.0.1:$NGROK_PORT"
echo ">> log: $NGROK_LOG   |   inspector: http://127.0.0.1:4040"
curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
  | python3 -c 'import sys,json
d=json.load(sys.stdin); t=d.get("tunnels",[])
print(">> public_url:", t[0]["public_url"]) if t else print(">> no tunnel — check the log")' 2>/dev/null \
  || echo ">> (not up yet — check $NGROK_LOG)"
