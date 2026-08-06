#!/bin/sh
# Entrypoint for LQABR Cloud Run services. SERVICE_KIND selects the process:
#   agent   -> ADK api_server exposing the agent (A2A/HTTP)
#   webhook -> FastAPI webhook app via uvicorn
set -e

case "${SERVICE_KIND}" in
  agent)
    exec adk api_server "agents/${AGENT_DIR}/src" --host 0.0.0.0 --port "${PORT}"
    ;;
  webhook)
    # APP_MODULE parametrizes which module exposes the FastAPI `app`.
    # Default stays webhook_app:app (email, scheduling); text_voice Rev 5
    # retired webhook_app.py — its app lives in tools.py, so its Cloud Run
    # service deploys with APP_MODULE=tools:app (a runtime env var, no
    # image rebuild needed to switch).
    cd "agents/${AGENT_DIR}/src"
    exec uvicorn "${APP_MODULE:-webhook_app:app}" --host 0.0.0.0 --port "${PORT}"
    ;;
  *)
    echo "unknown SERVICE_KIND '${SERVICE_KIND}' (agent|webhook)" >&2
    exit 1
    ;;
esac
