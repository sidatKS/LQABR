"""HTTP ingress — the gateway's single entry point, and its only entry point.

Rev 3: *The gateway is the only entry point for HubSpot events. No
agent-to-agent traffic passes through it.*

This module is deliberately thin. It owns the HTTP contract with HubSpot and
the order of operations — nothing else:

    ingress -> router (Step 2) -> audit (Step 3) -> dispatch (Step 4)

Endpoints
---------
    POST {ingress.path}   HubSpot webhook. Default /hubspot/events.
    GET  /healthz         liveness: the process is up.
    GET  /readyz          readiness: every enabled agent endpoint resolves.
    GET  /metrics         handoff counters (JSON at MVP).
    GET  /                what this service is, and what it refuses to carry.

Response contract with HubSpot
------------------------------
HubSpot retries a non-2xx delivery with an incremented ``attemptNumber``, so the
status code is a control signal, not decoration:

    200  every event reached a terminal outcome — dispatched, or deliberately
         discarded because its value was not the routing condition.
    401  signature missing / stale / wrong.
    400  malformed envelope.  413  batch over 100 events.
    503  at least one event matched a route but could not be handed off (no
         endpoint, or dispatch failed after retries). Those event ids are
         *not* recorded in the dedupe store, so the redelivery is a real
         second attempt. This is how "a lead is never silently dropped"
         actually holds at the HTTP layer.

Run locally:  uvicorn server:app --port 8080   (from agents/gateway/src)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- make lib/ importable ------------------------------------------------
# The runtime adapter lives at agents/gateway/lib/soloai. Rev 3 co-locates it
# in the gateway's own tree rather than installing it, so it is put on the path
# here instead of being pip-installed.
_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(_GATEWAY_ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(_GATEWAY_ROOT / "lib"))

from fastapi import FastAPI, Request, Response  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from soloai import RUNTIME, load_config, load_registry_document  # noqa: E402
from soloai.audit_hooks import AuditHooks, ProfileFieldLeak  # noqa: E402
from soloai.protocols import mcp as mcp_protocol  # noqa: E402
from soloai.protocols.http import (  # noqa: E402
    ConcurrencyGuard,
    IngressError,
    SignatureError,
    parse_batch,
    verify_v3_signature,
)

try:
    from .audit import GatewayAudit
    from .dispatch import Dispatcher
    from .router import AgentRegistry, DedupeStore, Router
except ImportError:  # pragma: no cover - uvicorn run from inside src/
    from audit import GatewayAudit  # type: ignore
    from dispatch import Dispatcher  # type: ignore
    from router import AgentRegistry, DedupeStore, Router  # type: ignore


def create_app(
    config: Any = None,
    registry: Optional[AgentRegistry] = None,
    dispatcher: Optional[Dispatcher] = None,
    audit: Optional[GatewayAudit] = None,
    keep_records: bool = False,
) -> FastAPI:
    """Build the ingress app.

    Every collaborator is injectable so the tests exercise the real wiring with
    fake transports, rather than testing a parallel implementation.
    """
    config = config if config is not None else load_config()

    hooks = audit.hooks if audit is not None else AuditHooks.from_config(
        config, keep_records=keep_records)
    gateway_audit = audit or GatewayAudit(hooks)

    registry = registry or AgentRegistry.from_document(load_registry_document())

    dedupe = DedupeStore(
        ttl_seconds=int(config.get("dedupe.ttl_seconds", 900)),
        max_entries=int(config.get("dedupe.max_entries", 10000)),
    )
    router = Router(
        registry=registry,
        dedupe=dedupe,
        loop_guard_mode=str(config.get("loop_guard.mode", "self_trigger")),
        ignored_change_sources=config.get("loop_guard.ignored_change_sources", ["API"]),
        dedupe_enabled=bool(config.get("dedupe.enabled", True)),
    )
    dispatcher = dispatcher or Dispatcher.from_config(config, gateway_audit)

    ingress_path = str(config.get("gateway.ingress.path", "/hubspot/events"))
    max_batch = int(config.get("gateway.ingress.max_batch_size", 100))
    concurrency = ConcurrencyGuard(int(config.get("gateway.ingress.max_concurrent_requests", 10)))

    signature_enabled = bool(config.get("gateway.ingress.signature.enabled", True))
    signature_header = str(config.get("gateway.ingress.signature.header",
                                      "X-HubSpot-Signature-v3"))
    timestamp_header = str(config.get("gateway.ingress.signature.timestamp_header",
                                      "X-HubSpot-Request-Timestamp"))
    signature_max_age = int(config.get("gateway.ingress.signature.max_age_seconds", 300))

    # Fails fast at startup if someone configured the gateway to proxy profiles.
    mcp_endpoint = mcp_protocol.endpoint_from_config(config)

    app = FastAPI(
        title="LQABR Agent Gateway",
        version=str(config.get("gateway.version", "0.1.0")),
        description=(
            "Single ingress for HubSpot triggers. Carries a trigger_id only — "
            "no lead-profile data crosses this service."
        ),
    )
    app.state.config = config
    app.state.router = router
    app.state.dispatcher = dispatcher
    app.state.audit = gateway_audit
    app.state.registry = registry
    app.state.concurrency = concurrency
    app.state.ingress_path = ingress_path

    def _config_problems() -> Dict[str, str]:
        """Misconfigurations that would silently reject every real webhook.

        Both of these fail *closed*: the gateway looks healthy and returns 401
        to HubSpot forever. Worth surfacing at startup and on /readyz rather
        than discovering it from a lead that never got contacted.
        """
        problems: Dict[str, str] = {}
        if signature_enabled:
            if not os.environ.get("HUBSPOT_APP_SECRET", "").strip():
                problems["HUBSPOT_APP_SECRET"] = (
                    "unset while ingress.signature.enabled is true — every webhook "
                    "will be rejected with 401")
            if not os.environ.get("LQABR_GATEWAY_PUBLIC_URL", "").strip():
                problems["LQABR_GATEWAY_PUBLIC_URL"] = (
                    "unset — behind a TLS-terminating proxy the reconstructed URI "
                    "will not match the one HubSpot signed, so signatures will fail")
        return problems

    # ------------------------------------------------------------- lifecycle
    @app.on_event("startup")
    def _startup() -> None:
        for key, problem in _config_problems().items():
            gateway_audit.record_config_error(f"{key}: {problem}")
        gateway_audit.record_startup(
            runtime=RUNTIME,
            registry_health=registry.health(),
            ingress_path=ingress_path,
            concurrency_limit=concurrency.limit,
            chunk_size_hint=(mcp_endpoint.chunk_size_hint if mcp_endpoint else None),
        )

    # --------------------------------------------------------------- probes
    @app.get("/healthz")
    def healthz() -> Dict[str, Any]:
        return {
            "status": "ok",
            "service": config.get("gateway.name", "agent-gateway"),
            "version": config.get("gateway.version", "0.1.0"),
            "runtime": RUNTIME["name"],
        }

    @app.get("/readyz")
    def readyz() -> Response:
        health = registry.health()
        problems = _config_problems()
        ready = all(item.get("ready") for item in health.values()) and not problems
        content: Dict[str, Any] = {
            "status": "ready" if ready else "not-ready", "agents": health}
        if problems:
            content["config"] = problems
        return JSONResponse(status_code=200 if ready else 503, content=content)

    @app.get("/metrics")
    def metrics() -> Dict[str, Any]:
        return {
            "handoff": gateway_audit.metrics.as_dict(),
            "ingress": concurrency.snapshot(),
            "dedupe_entries": len(router.dedupe),
        }

    @app.get("/")
    def describe() -> Dict[str, Any]:
        return {
            "service": "LQABR Agent Gateway",
            "ingress": ingress_path,
            "carries": "trigger_id only",
            "never_carries": "lead-profile data — agents resolve profiles from HubSpot over MCP",
            "runtime": RUNTIME,
            "routes": [
                {"id": r.id, "property": r.property_name,
                 "values": list(r.values), "agent": r.agent}
                for r in registry.routes
            ],
        }

    # -------------------------------------------------------------- ingress
    # Bounds how many batches are in flight at once. HubSpot's throttle is 10;
    # this is us holding that bound ourselves rather than trusting it. Requests
    # over the limit wait their turn — Rev 3 says absorb the concurrency, not
    # shed it.
    admission = asyncio.Semaphore(concurrency.limit)

    def _process(raw_body: bytes, method: str, uri: str, headers: Dict[str, str],
                 source_ip: Optional[str], run_id: str) -> Tuple[int, Dict[str, Any]]:
        """The whole synchronous pipeline: verify -> route -> audit -> dispatch.

        Runs in a worker thread, NOT on the event loop. It calls blocking
        ``requests.post`` and ``time.sleep`` (retry backoff); left on the loop
        those serialise every request in the process — measured at 30s wall
        clock for ten concurrent 3-event batches against a 1s-per-agent stub,
        with /healthz starved for 29s and the container's own HEALTHCHECK
        (timeout 3s) killing it mid-batch.
        """
        started = time.perf_counter()
        concurrency.acquire()
        try:
            # --- signature -------------------------------------------------
            if signature_enabled:
                try:
                    verify_v3_signature(
                        secret=os.environ.get("HUBSPOT_APP_SECRET", ""),
                        method=method,
                        uri=uri,
                        body=raw_body.decode("utf-8", errors="replace"),
                        timestamp=headers.get(timestamp_header.lower(), ""),
                        signature=headers.get(signature_header.lower(), ""),
                        max_age_seconds=signature_max_age,
                    )
                except SignatureError as exc:
                    gateway_audit.record_ingress_rejected(
                        run_id, reason=str(exc), status_code=exc.status_code,
                        source_ip=source_ip, endpoint=ingress_path)
                    return 401, {"run_id": run_id, "error": "unauthorized"}

            # --- envelope --------------------------------------------------
            try:
                payload = json.loads(raw_body or b"")
                batch = parse_batch(payload, raw_body, max_batch_size=max_batch)
            except json.JSONDecodeError as exc:
                gateway_audit.record_ingress_rejected(
                    run_id, reason=f"invalid JSON: {exc}", status_code=400,
                    source_ip=source_ip, endpoint=ingress_path)
                return 400, {"run_id": run_id, "error": "invalid JSON body"}
            except IngressError as exc:
                gateway_audit.record_ingress_rejected(
                    run_id, reason=str(exc), status_code=exc.status_code,
                    source_ip=source_ip, endpoint=ingress_path)
                return exc.status_code, {"run_id": run_id, "error": str(exc)}

            gateway_audit.record_ingress(
                run_id, source_ip=source_ip, endpoint=ingress_path,
                method=method, event_count=batch.size,
                payload_bytes=batch.raw_size_bytes, signature_verified=signature_enabled,
                concurrency=concurrency.snapshot(),
            )

            # --- Step 2: route --------------------------------------------
            result = router.route_batch(batch.events)
            for decision in result.decisions:
                gateway_audit.record_decision(run_id, decision)
            gateway_audit.record_discards(run_id, result.discarded)
            gateway_audit.record_routing_errors(run_id, result.errors)

            # --- Step 4: hand off -----------------------------------------
            # dispatch_all, not a bare loop: it isolates a per-hand-off
            # exception so one bad decision cannot abandon the rest of the
            # batch undispatched.
            outcomes = dispatcher.dispatch_all(result.decisions, run_id)
            dispatched: List[Dict[str, Any]] = []
            ok_count = failed_count = 0
            for decision, outcome in zip(result.decisions, outcomes):
                dispatched.append(outcome.as_dict())
                if outcome.ok:
                    ok_count += 1
                    # The ONLY thing that ever enters the dedupe store: a
                    # hand-off that actually landed. A redelivery of this event
                    # is a duplicate from here on; a failure is not remembered,
                    # so its redelivery gets a real second attempt.
                    router.dedupe.remember(decision.event_id)
                else:
                    failed_count += 1

            summary = gateway_audit.record_run_summary(
                run_id, result, ok_count, failed_count,
                duration_ms=(time.perf_counter() - started) * 1000)

            # Anything that matched but did not land must make HubSpot retry.
            needs_retry = bool(result.errors) or failed_count > 0
            body = {
                "run_id": run_id,
                "accepted": batch.size,
                **summary,
                "dispatched": dispatched,
                "routing_errors": [e.as_dict() for e in result.errors],
            }
            return (503 if needs_retry else 200), body

        except ProfileFieldLeak as exc:
            # The trigger-only guarantee was violated. 500 loudly; do not
            # pretend the run succeeded.
            gateway_audit.record_exception(run_id, exc, where="ingress")
            return 500, {"run_id": run_id, "error": "profile data guard tripped"}
        except Exception as exc:  # noqa: BLE001
            gateway_audit.record_exception(run_id, exc, where="ingress")
            return 500, {"run_id": run_id, "error": "internal gateway error"}
        finally:
            concurrency.release()

    @app.post(ingress_path)
    async def hubspot_ingress(request: Request) -> Response:
        run_id = gateway_audit.new_run_id()
        raw_body = await request.body()
        # Snapshot everything the pipeline needs before leaving the loop: the
        # Request object is not safe to touch from a worker thread.
        headers = {k.lower(): v for k, v in request.headers.items()}
        source_ip = request.client.host if request.client else None
        uri = _public_uri(request)
        method = request.method

        async with admission:
            status, body = await run_in_threadpool(
                _process, raw_body, method, uri, headers, source_ip, run_id)
        return JSONResponse(status_code=status, content=body)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        # The file sink and the pooled HTTP session both hold OS resources.
        try:
            hooks.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass

    return app


def _public_uri(request: Request) -> str:
    """The URI HubSpot signed.

    Cloud Run terminates TLS and rewrites the host, so ``request.url`` can
    differ from what HubSpot hashed. ``LQABR_GATEWAY_PUBLIC_URL`` pins the
    externally visible origin; without it, signature verification would fail
    for correct requests behind the proxy.
    """
    path = request.url.path
    query = f"?{request.url.query}" if request.url.query else ""

    public_origin = os.environ.get("LQABR_GATEWAY_PUBLIC_URL", "").rstrip("/")
    if public_origin:
        return f"{public_origin}{path}{query}"

    # No pinned origin: fall back to the forwarded headers, because Cloud Run
    # terminates TLS and `request.url` would say http:// while HubSpot signed
    # https://. Still a fallback, not a substitute — the proxy headers are
    # caller-controlled, so /readyz reports the missing pin as a problem.
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto.split(',')[0].strip()}://{forwarded_host.split(',')[0].strip()}{path}{query}"
    return str(request.url)


# Module-level app for `uvicorn server:app` and Cloud Run.
# Guarded so an import in a test environment without config never crashes at
# collection time — the tests build their own app via create_app().
try:  # pragma: no cover - exercised by deployment, not by unit tests
    app = create_app()
except Exception as _startup_error:  # noqa: BLE001
    app = FastAPI(title="LQABR Agent Gateway (failed to start)")

    @app.get("/healthz")
    def _broken_healthz() -> Response:  # type: ignore[misc]
        return JSONResponse(status_code=503, content={
            "status": "configuration-error",
            "error": str(_startup_error),
        })
