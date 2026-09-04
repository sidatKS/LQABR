#!/usr/bin/env python3
"""Turn a live Cloud Run service export into its sidecar version.

    gcloud run services describe lqabr-dev-research --region us-central1 \
        --format=export > service-live.yaml
    python3 add_collector.py service-live.yaml <image> > service.yaml
    gcloud run services replace service.yaml --region us-central1

Why a patcher and not a checked-in service.yaml: `services replace` makes the
service IDENTICAL to the file. Anything absent from it is deleted - the
service account, `--ingress internal`, the VPC egress, every env var. A
hand-written file silently drops whatever nobody remembered. This starts from
what is actually deployed and only ADDS.

Idempotent: running it on an already-patched file changes nothing.
"""
from __future__ import annotations
import sys, yaml

SECRET    = "lqabr-otel-collector-config"
PROJECT   = "ldqfingsrv-dev"
COLLECTOR = ("us-docker.pkg.dev/cloud-ops-agents-artifacts/"
             "google-cloud-opentelemetry-collector/otelcol-google:0.159.0")
MOUNT     = "/etc/otelcol-google"


def main(path: str, app_image: str) -> None:
    doc = yaml.safe_load(open(path))

    # `replace` rejects server-owned fields. The export usually omits them;
    # a describe without --format=export does not.
    doc.pop("status", None)
    meta = doc.setdefault("metadata", {})
    for k in ("resourceVersion", "uid", "creationTimestamp", "generation",
              "selfLink"):
        meta.pop(k, None)

    # Multi-container is not GA-flagged on the service object.
    meta.setdefault("annotations", {})["run.googleapis.com/launch-stage"] = "ALPHA"

    tmpl = doc["spec"]["template"]
    ann  = tmpl.setdefault("metadata", {}).setdefault("annotations", {})
    # app waits for collector to pass its startup probe. Without this the app
    # can boot, export to a socket nobody is listening on, and lose the first
    # seconds of every cold start.
    ann["run.googleapis.com/container-dependencies"] = "{app:[collector]}"
    ann["run.googleapis.com/secrets"] = (
        f"{SECRET}:projects/{PROJECT}/secrets/{SECRET}")
    # CPU is throttled between requests by default, so the collector's batch
    # timer never fires and queued spans die at instance shutdown.
    ann["run.googleapis.com/cpu-throttling"] = "false"

    spec = tmpl["spec"]
    containers = spec["containers"]

    app = next((c for c in containers if c.get("name") == "app"), containers[0])
    app["name"] = "app"                      # must match container-dependencies
    app["image"] = app_image
    env = {e["name"]: e for e in app.setdefault("env", [])}
    for name, value in (
        # localhost, not a service name: sidecars share one network namespace.
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
        ("OTEL_SERVICE_NAME", doc["metadata"]["name"]),
        ("OTEL_TRACES_EXPORTER", "otlp"),
        ("OTEL_METRICS_EXPORTER", "otlp"),
    ):
        env.setdefault(name, {"name": name})["value"] = value
    app["env"] = list(env.values())

    if not any(c.get("name") == "collector" for c in containers):
        containers.append({
            "name": "collector",
            "image": COLLECTOR,
            "args": [f"--config={MOUNT}/config.yaml"],
            # NO ports block. Only the ingress container may declare one;
            # a second `ports` fails validation.
            "startupProbe":  {"httpGet": {"path": "/", "port": 13133},
                              "timeoutSeconds": 30, "periodSeconds": 30,
                              "failureThreshold": 3},
            "livenessProbe": {"httpGet": {"path": "/", "port": 13133},
                              "timeoutSeconds": 30, "periodSeconds": 30},
            "volumeMounts": [{"name": "otel-config", "mountPath": MOUNT}],
        })

    vols = spec.setdefault("volumes", [])
    if not any(v.get("name") == "otel-config" for v in vols):
        vols.append({"name": "otel-config",
                     "secret": {"secretName": SECRET,
                                "items": [{"key": "latest",
                                           "path": "config.yaml"}]}})

    yaml.safe_dump(doc, sys.stdout, sort_keys=False, default_flow_style=False)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
