#!/usr/bin/env bash
# verify.sh — post-deploy smoke checks. Read-only. Self-contained: ./config.sh.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

echo "== services (${PROJECT_ID})"
fail=0
for e in "${LQABR_SERVICES[@]}"; do
  IFS='|' read -r name dir kind exposure <<<"$e"
  url="$(gcloud run services describe "$name" --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)' 2>/dev/null)" || url=""
  if [[ -n "$url" ]]; then printf '  OK   %-24s %-8s %s\n' "$name" "$exposure" "$url"
  else printf '  MISS %-24s %-8s (not deployed)\n' "$name" "$exposure"; fail=1; fi
done

echo "== Gateway public invoker (expect allUsers)"
GTWY="$(for e in "${LQABR_SERVICES[@]}"; do IFS='|' read -r n d k x <<<"$e"; [[ "$d" == "gateway" ]] && echo "$n"; done)"
gcloud run services get-iam-policy "${GTWY}" --region "${REGION}" --project "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.role=roles/run.invoker AND bindings.members=allUsers" \
  --format="value(bindings.role)" | grep -q run.invoker && echo "  OK   allUsers can invoke ${GTWY}" || { echo "  MISS allUsers binding on ${GTWY}"; fail=1; }

echo "== runtime SA project run.invoker (agent-to-agent OIDC)"
gcloud projects get-iam-policy "${PROJECT_ID}" --flatten="bindings[].members" \
  --filter="bindings.role=roles/run.invoker AND bindings.members=serviceAccount:${AGENT_SA}" \
  --format="value(bindings.role)" | grep -q run.invoker && echo "  OK   ${AGENT_SA} has run.invoker" || { echo "  MISS run.invoker on ${AGENT_SA}"; fail=1; }

echo "== Gateway health"
GURL="$(gcloud run services describe "${GTWY}" --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)' 2>/dev/null)" || GURL=""
[[ -n "$GURL" ]] && { code="$(curl -s -o /dev/null -w '%{http_code}' "${GURL}/healthz" || true)"; echo "  Gateway ${GURL}/healthz -> HTTP ${code}"; }

[[ "$fail" == "0" ]] && echo "verify: all checks passed." || { echo "verify: some checks failed."; exit 1; }
