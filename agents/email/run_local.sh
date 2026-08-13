#!/usr/bin/env bash
# Run the LQABR Email Agent locally — WSL / Linux.
#
#   bash agents/email/run_local.sh preflight   # check the environment, send nothing
#   bash agents/email/run_local.sh probe       # what IS the lead-selection property?
#   bash agents/email/run_local.sh serve       # start the service on :8080
#   bash agents/email/run_local.sh dryrun <object_id>   # construct every email, send none
#   bash agents/email/run_local.sh test        # the offline suite
#   bash agents/email/run_local.sh adk         # interactive agent (terminal)
#   bash agents/email/run_local.sh web         # ADK browser dev UI
#
# Run it from the REPO ROOT. It uses the existing .venv (created in WSL with
# python3 -m venv .venv); it does not create or modify one silently.
#
# Credentials come from Secret Manager, not this shell — see
# docs/EMAIL_AGENT_ENV_VARS.md. That needs Application Default Credentials:
#
#     gcloud auth application-default login
#
# Nothing here sends email. `serve` exposes the endpoints; a real campaign is
# an explicit POST you make yourself.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

VENV="${REPO_ROOT}/.venv"
PORT="${PORT:-8080}"

die() { echo "run_local: $*" >&2; exit 1; }

[[ -d "${VENV}" ]] || die "no .venv at ${VENV}. Create it in WSL:
    python3 -m venv .venv && source .venv/bin/activate
    pip install -e 'packages/lqabr_core[gcp]' -r agents/email/requirements.txt"

[[ -f "${VENV}/bin/activate" ]] || die "${VENV} has no bin/activate — it looks like a
Windows venv (Scripts/). WSL needs its own: rm -rf .venv and recreate it inside WSL."

# shellcheck disable=SC1091
source "${VENV}/bin/activate"

deps_ok() {
  python - <<'PY' 2>/dev/null
import importlib.util as u, sys
need = ["fastapi", "uvicorn", "requests", "dotenv", "lqabr_core",
        "google.cloud.secretmanager", "pytest"]
sys.exit(0 if all(u.find_spec(m) for m in need) else 1)
PY
}

if ! deps_ok; then
  echo "run_local: installing dependencies into .venv"
  pip install -q -e "packages/lqabr_core[gcp]" -r agents/email/requirements.txt \
              pytest httpx python-multipart
fi

case "${1:-preflight}" in

  preflight)
    exec python agents/email/preflight.py
    ;;

  probe)
    # Read-only investigation of the HubSpot schema — what the lead-selection
    # property should actually be. Writes nothing, sends nothing.
    exec python agents/email/probe_hubspot.py
    ;;

  test)
    exec python -m pytest -q
    ;;

  serve)
    echo "run_local: preflight first (nothing is sent)"
    python agents/email/preflight.py || die "preflight failed — fix the above before serving"
    cat <<EOF

run_local: starting on http://127.0.0.1:${PORT}

  health      curl -s localhost:${PORT}/health
  routes      curl -s localhost:${PORT}/
  DRY RUN     curl -s -X POST localhost:${PORT}/hubspot/campaign \\
                -H 'Content-Type: application/json' \\
                -d '{"object_id":"<id>","limit":1,"dry_run":true}'

  A REAL SEND is the same call with "dry_run": false. Do the dry run first.

EOF
    cd agents/email/src
    exec python -m uvicorn service_app:app --host 0.0.0.0 --port "${PORT}" --reload
    ;;

  dryrun)
    OBJECT_ID="${2:-}"
    [[ -n "${OBJECT_ID}" ]] || die "usage: run_local.sh dryrun <object_id>"
    echo "run_local: dry run for ${OBJECT_ID} — constructs every email, sends none"
    cd agents/email/src
    exec python - "${OBJECT_ID}" <<'PY'
import json, sys, logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
import outreach
result = outreach.run_campaign(sys.argv[1], limit=1, dry_run=True)
print(json.dumps(result, indent=2, default=str))
PY
    ;;

  adk|web)
    # The ADK entry points. agent.py loads agents/email/.env before importing
    # email_agent, so these see the same configuration uvicorn does.
    #
    # NOTE these do NOT gate on preflight. That is deliberate: the tools that
    # do not need the campaign-selection property — preview_email,
    # get_lead_status — are exactly what you want while that property is still
    # missing, and they send nothing. run_email_campaign will fail loudly.
    command -v adk >/dev/null || die "no 'adk' in the venv. pip install 'google-adk[extensions]==2.3.0'"

    if [[ "${1}" == "web" ]]; then
      ADK_PORT="${ADK_PORT:-8000}"
      cat <<EOF

run_local: adk web  ->  http://localhost:${ADK_PORT}

  Open that in your Windows browser. Bound to 0.0.0.0 so it works whether or
  not WSL localhost-forwarding is on; if localhost does not resolve, use the
  WSL address:  http://\$(hostname -I | awk '{print \$1}'):${ADK_PORT}

  The agent appears as the app "src".

  Safe to click:  preview_email, get_lead_status, list_email_queue
  SENDS A REAL EMAIL:  send_outreach_email  — it is not a dry run.
  run_email_campaign fails with a named crm-error until the selection
  property exists; use dry_run=true when it does.

EOF
      exec adk web agents/email/src --host 0.0.0.0 --port "${ADK_PORT}"
    fi

    cat <<EOF

run_local: adk run agents/email/src

  Read-and-construct, sends nothing:
    preview the email for stephen.m@tekninjas.com
    what is the status of stephen.m@tekninjas.com

EOF
    exec adk run agents/email/src
    ;;

  *)
    die "unknown command '${1}'. Use: preflight | probe | serve | dryrun <object_id> | test | adk | web"
    ;;
esac
