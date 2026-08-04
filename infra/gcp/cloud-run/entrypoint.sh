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
    cd "agents/${AGENT_DIR}/src"
    exec uvicorn webhook_app:app --host 0.0.0.0 --port "${PORT}"
    ;;
  *)
    echo "unknown SERVICE_KIND '${SERVICE_KIND}' (agent|webhook)" >&2
    exit 1
    ;;
esac
