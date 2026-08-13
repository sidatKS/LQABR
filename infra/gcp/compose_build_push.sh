#!/usr/bin/env bash
# =====================================================================
# compose_build_push.sh — build the LQABR images with Docker Compose and,
# ONLY when explicitly asked, push them to Google Artifact Registry.
#
# BUILD IS THE DEFAULT. PUSHING REQUIRES PUSH=1.
# Nothing reaches the registry by accident: a plain run builds and stops,
# printing exactly what it *would* push. Swaroop reviews the spec first
# (docs/IMAGE_BUILD_SPEC.md); the push happens after that approval.
#
# Usage, from the REPO ROOT:
#     bash infra/gcp/compose_build_push.sh                      # build only
#     bash infra/gcp/compose_build_push.sh email-agent
#     VERSION=0.1.0 bash infra/gcp/compose_build_push.sh email-agent
#
#     PUSH=1 bash infra/gcp/compose_build_push.sh email-agent
#                                                               # build + push
#
# Prerequisites for the push (once per machine):
#     gcloud auth login
#     gcloud auth configure-docker us-central1-docker.pkg.dev
#     gcloud artifacts repositories create lqabr \
#         --repository-format=docker --location=us-central1 \
#         --project=ldqfingsrv-dev            # if it does not exist yet
#
# Why :latest is pushed by an explicit loop rather than left to Compose:
# `docker compose push` publishes the `image:` value; whether it also
# publishes the extra `build.tags` entries depends on the Compose version.
# Tagging and pushing here is version-independent, and Swaroop asked
# specifically that a pullable :latest always exist.
# =====================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# The compose file lives in infra/ (build contexts inside it are `..`, i.e.
# the repo root), so every compose call must name it explicitly.
COMPOSE_FILE="infra/docker-compose.yml"

export VERSION="${VERSION:-0.1.0}"
export IMAGE_BASE="${IMAGE_BASE:-us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr}"
export IMAGE_PREFIX="${IMAGE_PREFIX:-lqabr-dev}"
PUSH="${PUSH:-0}"

echo "== registry : ${IMAGE_BASE}"
echo "== prefix   : ${IMAGE_PREFIX}"
echo "== version  : ${VERSION}"
echo "== push     : $([ "${PUSH}" = "1" ] && echo 'YES' || echo 'no (build only - set PUSH=1 to publish)')"
echo

echo "== building"
docker compose -f "${COMPOSE_FILE}" build "$@"

images="$(docker compose -f "${COMPOSE_FILE}" config --images "$@")"

if [ "${PUSH}" != "1" ]; then
  echo
  echo "== built, NOT pushed. These would be published with PUSH=1:"
  for image in ${images}; do
    echo "   ${image}"
    echo "   ${image%:*}:latest"
  done
  echo
  echo "   Inspect one before approving:"
  echo "     docker run --rm -it --entrypoint sh $(echo "${images}" | head -1)"
  exit 0
fi

echo
echo "== pushing"
for image in ${images}; do
  latest="${image%:*}:latest"
  docker tag "${image}" "${latest}"
  docker push "${image}"
  docker push "${latest}"
  echo "   pushed ${image}"
  echo "   pushed ${latest}"
done

echo
echo "done. Verify with:"
echo "  gcloud artifacts docker images list ${IMAGE_BASE} --include-tags --project ldqfingsrv-dev"
