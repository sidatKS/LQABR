#!/usr/bin/env bash
# Deploy to Cloud Run. Scales to zero; nothing here stays warm.
#
# Rendered as a Service YAML and applied with `gcloud run services replace`,
# not `gcloud run deploy`: the OpenTelemetry collector runs as a SECOND
# container in this same service, and a multi-container service cannot be
# expressed in deploy flags.
#
# `replace` does not touch the invoker IAM binding, so the service stays
# authenticated exactly as `--no-allow-unauthenticated` left it.
#
# RENDER_ONLY=1 prints the YAML and exits, so it can be reviewed before the
# first apply.
set -euo pipefail
source "$(dirname "$0")/config.sh"

RENDERED="$(mktemp)"
trap 'rm -f "${RENDERED}"' EXIT

cat > "${RENDERED}" <<YAML
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: ${SERVICE_NAME}
  annotations:
    run.googleapis.com/launch-stage: ALPHA
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: "0"
        autoscaling.knative.dev/maxScale: "3"
        # The collector starts before the app, so the app's first spans have
        # somewhere to go.
        run.googleapis.com/container-dependencies: "{app:[collector]}"
        run.googleapis.com/secrets: '${OTEL_CONFIG_SECRET}:projects/${PROJECT_ID}/secrets/${OTEL_CONFIG_SECRET}'
        # Without this the collector's 5s batch flush timer is never scheduled
        # between requests and buffered telemetry is dropped at shutdown. It
        # also moves the service to instance-based billing.
        run.googleapis.com/cpu-throttling: "false"
    spec:
      serviceAccountName: ${RUNTIME_SA}
      timeoutSeconds: 300
      containers:
      - name: app
        image: ${IMAGE}:${IMAGE_TAG}
        ports:
        - containerPort: 8080
        resources:
          limits:
            cpu: "${CPU}"
            memory: ${MEMORY}
        env:
        - name: LQABR_SUMMARY_MCP_BASE_URL
          value: "${LQABR_SUMMARY_MCP_BASE_URL}"
        - name: LQABR_SUMMARY_MODEL
          value: "${LQABR_SUMMARY_MODEL}"
        - name: LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE
          value: "${LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE}"
        - name: LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY
          value: "${LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY}"
        - name: LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY
          value: "${LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY}"
        - name: LQABR_SUMMARY_ROUTES
          value: "${LQABR_SUMMARY_ROUTES}"
        - name: LQABR_SUMMARY_DRY_RUN
          value: "${LQABR_SUMMARY_DRY_RUN}"
        - name: LQABR_SUMMARY_MCP_STARTUP_CHECK
          value: "${LQABR_SUMMARY_MCP_STARTUP_CHECK}"
        - name: LQABR_SUMMARY_SECRETS_SOURCE
          value: "secret_manager"
        - name: LQABR_SUMMARY_GCP_PROJECT
          value: "${PROJECT_ID}"
        # Read by the collector config and by obs.py, which needs it to build
        # the logging.googleapis.com/trace field.
        - name: GOOGLE_CLOUD_PROJECT
          value: "${PROJECT_ID}"
        # Setting the endpoint is what switches export on; unset, obs.py is a
        # no-op and the agent behaves exactly as it did before.
        - name: OTEL_EXPORTER_OTLP_ENDPOINT
          value: "http://localhost:4317"
        - name: OTEL_EXPORTER_OTLP_PROTOCOL
          value: "grpc"
        - name: OTEL_SERVICE_NAME
          value: "${OTEL_SERVICE_NAME}"
      - name: collector
        image: ${OTEL_COLLECTOR_IMAGE}
        args:
        - --config=/etc/otelcol-google/config.yaml
        # No ports block: only the ingress container may declare one. 13133 is
        # reached by the probes inside the instance.
        startupProbe:
          httpGet:
            path: /
            port: 13133
          timeoutSeconds: 30
          periodSeconds: 30
        livenessProbe:
          httpGet:
            path: /
            port: 13133
          timeoutSeconds: 30
          periodSeconds: 30
        volumeMounts:
        - name: otel-config
          mountPath: /etc/otelcol-google/
      volumes:
      - name: otel-config
        secret:
          secretName: ${OTEL_CONFIG_SECRET}
          items:
          - key: latest
            path: config.yaml
YAML

if [[ "${RENDER_ONLY:-0}" == "1" ]]; then
  cat "${RENDERED}"
  exit 0
fi

gcloud run services replace "${RENDERED}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}"

URL="$(gcloud run services describe "${SERVICE_NAME}" --project "${PROJECT_ID}" \
        --region "${REGION}" --format='value(status.url)')"

cat <<INFO

deployed: ${URL}

  health:      curl -H "Authorization: Bearer \$(gcloud auth print-identity-token)" ${URL}/health
  MCP surface: curl -H "Authorization: Bearer \$(gcloud auth print-identity-token)" ${URL}/mcp/tools

  collector:   gcloud logging read 'resource.type="cloud_run_revision"
                 AND resource.labels.service_name="${SERVICE_NAME}"
                 AND labels."run.googleapis.com/container_name"="collector"' \\
                 --limit=30 --freshness=1h --project=${PROJECT_ID}

  Point the gateway at it (only if you route summaries through the gateway):
    export LQABR_SUMMARY_AGENT_URL=${URL}

  DRY RUN is ${LQABR_SUMMARY_DRY_RUN}. Set LQABR_SUMMARY_DRY_RUN=0 and redeploy
  once /mcp/tools shows the write tool and a dry run has produced the summary
  you expect.
INFO
