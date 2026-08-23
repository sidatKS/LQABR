#!/usr/bin/env bash
# Hourly ADC refresh for the local HubSpot MCP container.
# `gcloud auth application-default login` mints a NEW adc.json (new inode), and
# the container's :ro bind-mount pins the old one — so we recreate the container.
set -euo pipefail
cd "$(dirname "$0")/.."

echo ">> re-authenticating ADC (complete the browser/device flow)"
gcloud auth application-default login

echo ">> recreating the MCP container with the fresh adc.json"
bash "$(dirname "$0")/run.sh"
echo ">> tip: token resolves on the first tool call; logs/mcp/hubspot.log will show 'secret_manager_access ... 200 ok'"
