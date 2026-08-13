#!/bin/sh
# Entrypoint for LQABR Cloud Run services. SERVICE_KIND selects the process:
#   service -> the agent's OWN FastAPI surface (uvicorn service_app:app)
#   agent   -> generic ADK api_server exposing root_agent (A2A/HTTP)
#   webhook -> provider webhook app only (uvicorn webhook_app:app)
#
# `service` is what the email agent deploys as. The gateway dispatches a
# DOMAIN call (POST /hubspot/campaign); the ADK api_server cannot parse one.
# Its contract is POST /run with an ADK envelope after a session has been
# created, and it answers /health but not /healthz. text_voice is deployed
# with a domain surface, so email must be too — otherwise the gateway needs
# two different dispatch shapes for two stage agents.
# See agents/email/src/service_app.py.
#
# `agent` remains for the agents that do not have a domain surface yet, and
# for callers that genuinely speak ADK.
set -e

case "${SERVICE_KIND}" in
  service)
    cd "agents/${AGENT_DIR}/src"
    exec uvicorn service_app:app --host 0.0.0.0 --port "${PORT}"
    ;;
  agent)
    exec adk api_server "agents/${AGENT_DIR}/src" --host 0.0.0.0 --port "${PORT}"
    ;;
  webhook)
    cd "agents/${AGENT_DIR}/src"
    exec uvicorn webhook_app:app --host 0.0.0.0 --port "${PORT}"
    ;;
  *)
    echo "unknown SERVICE_KIND '${SERVICE_KIND}' (service|agent|webhook)" >&2
    exit 1
    ;;
esac
