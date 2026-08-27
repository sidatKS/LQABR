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
import logging
import asyncio
import json
import os
import sys
import time

from logging.handlers import TimedRotatingFileHandler
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- make lib/ importable ------------------------------------------------
# The runtime adapter lives at agents/gateway/lib/soloai. Rev 3 co-locates it
# in the gateway's own tree rather than installing it, so it is put on the path
# here instead of being pip-installed.
_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(_GATEWAY_ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(_GATEWAY_ROOT / "lib"))

# --- local development: load config/.env if python-dotenv is installed ------
# Deployed environments inject real environment variables (Cloud Run pulls them
# from Secret Manager), so this is a convenience for running locally and never
# a requirement. `override=False` means a real environment variable always wins
# over the file — otherwise a stale .env on a developer's machine could quietly
# beat the value the platform set.
try:  # pragma: no cover - depends on the local environment
    from dotenv import load_dotenv

    load_dotenv(_GATEWAY_ROOT / "config" / ".env", override=False)
except ImportError:
    pass

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

# Flat imports on purpose: production runs `uvicorn server:app` from src/ and
# the tests load these modules standalone — nothing imports src/ as a package.
from audit import GatewayAudit  # noqa: E402
from call_report import CallReportRelay, ReportRelayError, extract_correlation  # noqa: E402
from dispatch import Dispatcher  # noqa: E402
from router import AgentRegistry, DedupeStore, Router  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)



LOG_DIR = "C:\\Users\\SarojaNemmaluri\\LQABR\\logs"
LOG_FILE = os.path.join(LOG_DIR, "gateway.log")
AUDIT_LOG_FILE = os.path.join(LOG_DIR, "gateway_audit.log")
os.makedirs(LOG_DIR, exist_ok=True)

# 2. Define a common format for both console and file logs
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
formatter = logging.Formatter(LOG_FORMAT)

logger = logging.getLogger("app.gateway")

# Set up file handler for logging
file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=30)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)

file_handler.propagate = False  # Prevent log messages from being propagated to the root logger

logger.addHandler(file_handler)

audit_logger = logging.getLogger("app.audit")

audit_file_handler = TimedRotatingFileHandler(AUDIT_LOG_FILE, when="midnight", interval=1, backupCount=30)
audit_file_handler.setLevel(logging.INFO)
audit_file_handler.setLevel(logging.DEBUG)  # Set the desired log level for audit logs
audit_file_handler.setFormatter(formatter)

audit_file_handler.propagate = False  # Prevent log messages from being propagated to the root logger

audit_logger.addHandler(audit_file_handler)

def create_app(
    config: Any = None,
    registry: Optional[AgentRegistry] = None,
    dispatcher: Optional[Dispatcher] = None,
    audit: Optional[GatewayAudit] = None,
    audience: Optional[Any] = None,
    keep_records: bool = False,
) -> FastAPI:
    """Build the ingress app.

    Every collaborator is injectable so the tests exercise the real wiring with
    fake transports, rather than testing a parallel implementation.
    """
    config = config if config is not None else load_config()

    logger.info(" saroja gateway starting up: %s", {
        "runtime": RUNTIME,
        "config": config,
    })  
    hooks = audit.hooks if audit is not None else AuditHooks.from_config(
        config, keep_records=keep_records)
    gateway_audit = audit or GatewayAudit(hooks)

    audit_logger.debug(" debug audit gateway info : %s", {
            "runtime": RUNTIME,
            "gateway_audit": hooks,
        })  

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

    # Rev 5 audience resolution. Built only when enabled AND a HubSpot token is
    # present; without it the gateway falls back to Step-1 behaviour (an R-blog
    # decision dispatches as a single hand-off carrying the ticket id).
    if audience is None and bool(config.get("audience.enabled", True)):
        _hs_token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
        if _hs_token:
            from audience import AudienceResolver
            audience = AudienceResolver.from_config(config, _hs_token)

    ingress_path = str(config.get("gateway.ingress.path", "/hubspot/events"))
    max_batch = int(config.get("gateway.ingress.max_batch_size", 100))
    concurrency = ConcurrencyGuard(int(config.get("gateway.ingress.max_concurrent_requests", 10)))

    signature_enabled = bool(config.get("gateway.ingress.signature.enabled", True))
    signature_header = str(config.get("gateway.ingress.signature.header",
                                      "X-HubSpot-Signature-v3"))
    timestamp_header = str(config.get("gateway.ingress.signature.timestamp_header",
                                      "X-HubSpot-Request-Timestamp"))
    signature_max_age = int(config.get("gateway.ingress.signature.max_age_seconds", 300))

    # --- Vapi end-of-call report relay ----------------------------------
    # A second inbound path, deliberately outside the router. See
    # call_report.py for why it does not use the A2A/trigger machinery.
    report_path = str(config.get("vapi.report.path", "/call-report"))
    report_enabled = bool(config.get("vapi.report.enabled", True))
    report_verify_secret = bool(config.get("vapi.report.signature.enabled", True))
    report_relay = CallReportRelay(
        target_url=os.environ.get("LQABR_VOICE_REPORT_URL", "").strip(),
        secret=os.environ.get("LQABR_VAPI_WEBHOOK_SECRET", "").strip(),
        verify_secret=report_verify_secret,
        timeout_seconds=int(config.get("vapi.report.timeout_seconds", 30)),
    )

    # Fails fast at startup if someone configured the gateway to proxy profiles.
    mcp_endpoint = mcp_protocol.endpoint_from_config(config)

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
        if report_enabled:
            if not report_relay.target_url:
                problems["LQABR_VOICE_REPORT_URL"] = (
                    f"unset while {report_path} is enabled — Vapi end-of-call "
                    "reports have nowhere to be relayed and will 503")
            if report_verify_secret and not os.environ.get(
                    "LQABR_VAPI_WEBHOOK_SECRET", "").strip():
                problems["LQABR_VAPI_WEBHOOK_SECRET"] = (
                    "unset while vapi.report.signature.enabled is true — every "
                    "report will be rejected with 401. The gateway owns Vapi "
                    "authenticity; txtv does not verify it.")
        return problems

    # ------------------------------------------------------------- lifecycle
    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        for key, problem in _config_problems().items():
            gateway_audit.record_config_error(f"{key}: {problem}")
        gateway_audit.record_startup(
            runtime=RUNTIME,
            registry_health=registry.health(),
            ingress_path=ingress_path,
            concurrency_limit=concurrency.limit,
            chunk_size_hint=(mcp_endpoint.chunk_size_hint if mcp_endpoint else None),
        )
        yield
        # The file sink and the pooled HTTP session both hold OS resources.
        try:
            hooks.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass

    app = FastAPI(
        lifespan=_lifespan,
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
    app.state.audience = audience
    app.state.audit = gateway_audit
    app.state.registry = registry
    app.state.concurrency = concurrency
    app.state.ingress_path = ingress_path

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
            # The body HubSpot actually sent. Opt-in (LQABR_LOG_PAYLOADS=1),
            # so it costs nothing in production.
            gateway_audit.record_ingress_payload(run_id, payload)

            # --- Step 2: route --------------------------------------------
            result = router.route_batch(batch.events)
            # --- Step 4a (Rev 5): resolve the audience --------------------
            # Expand each R-blog-summary decision into one research hand-off per
            # lead. Non-blog decisions pass through. A read failure becomes a
            # routing error (503); a zero-lead ticket is logged and dropped.
            if audience is not None:
                result = audience.expand(result, run_id, gateway_audit)
            for decision in result.decisions:
                gateway_audit.record_decision(run_id, decision)
            gateway_audit.record_discards(run_id, result.discarded)
            gateway_audit.record_routing_errors(run_id, result.errors)

            # --- Step 4: hand off -----------------------------------------
            # dispatch_all, not a bare loop: it isolates a per-hand-off
            # exception so one bad decision cannot abandon the rest of the
            # batch undispatched.
            # Reserve every eventId BEFORE dispatching. HubSpot's response
            # budget is 5s; a slow agent means redeliveries land while the
            # first hand-off is still in flight, and a store written only on
            # success is empty when they check it — so the same lead goes to
            # the agent several times. Reserving up front closes that window.
            # Failures are released again below, so a retry still gets a real
            # second attempt.
            for decision in result.decisions:
                router.dedupe.remember(decision.event_id)

            # Research hand-offs produced by audience resolution are always
            # grouped per ticket, so an industry's leads travel in ONE call
            # (keyed by summary_ref_id). Every other agent follows dispatch.mode.
            _research = [d for d in result.decisions if d.summary_ref_id is not None]
            _others = [d for d in result.decisions if d.summary_ref_id is None]
            outcomes = []
            if _research:
                outcomes += dispatcher.dispatch_grouped(_research, run_id)
            if _others:
                if dispatcher.mode == "grouped":
                    outcomes += dispatcher.dispatch_grouped(_others, run_id)
                else:
                    outcomes += dispatcher.dispatch_all(_others, run_id)

            dispatched: List[Dict[str, Any]] = []
            ok_count = failed_count = 0
            # Each outcome carries the eventIds it covers — one when per_lead,
            # up to batch_size when grouped — so both modes share this loop and
            # neither can drift out of step with the reservations above.
            for outcome in outcomes:
                dispatched.append(outcome.as_dict())
                if outcome.ok:
                    ok_count += 1
                    # Reservation stands: a redelivery of these events is a
                    # duplicate from here on.
                else:
                    failed_count += 1
                    # Release them, so HubSpot's redelivery gets a real second
                    # attempt instead of being deduped away.
                    for event_id in outcome.event_ids:
                        router.dedupe.forget(event_id)

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

    # --------------------------------------------- vapi end-of-call report
    def _relay_report(raw_body: bytes, headers: Dict[str, str],
                      source_ip: Optional[str], run_id: str) -> Tuple[int, bytes, str]:
        """Authenticate, audit, forward. Blocking, so it runs off the loop.

        Note what is absent: no router, no dedupe, no A2A envelope, no payload
        guard. This is a proxy for one route, not a trigger hand-off. txtv
        performs Step 7/8 (classification, HubSpot writes) itself.
        """
        started = time.perf_counter()
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            payload = None  # not fatal: correlation ids are best-effort

        ids = extract_correlation(payload)
        try:
            status, body, content_type = report_relay.forward(raw_body, headers)
        except ReportRelayError as exc:
            gateway_audit.record_call_report_rejected(
                run_id, reason=str(exc), status_code=exc.status_code,
                source_ip=source_ip, endpoint=report_path)
            return exc.status_code, json.dumps(
                {"run_id": run_id, "error": str(exc)}).encode(), "application/json"

        gateway_audit.record_call_report(
            run_id, source_ip=source_ip, endpoint=report_path,
            payload_bytes=len(raw_body), call_id=ids["call_id"],
            hubspot_contact_id=ids["hubspot_contact_id"],
            secret_verified=report_verify_secret)
        gateway_audit.record_call_report_relayed(
            run_id, target=report_relay.target_url, status_code=status,
            latency_ms=(time.perf_counter() - started) * 1000,
            authenticated=bool(report_relay.target_url))
        return status, body, content_type

    if report_enabled:
        @app.post(report_path)
        async def vapi_call_report(request: Request) -> Response:
            run_id = gateway_audit.new_run_id()
            raw_body = await request.body()
            headers = {k.lower(): v for k, v in request.headers.items()}
            source_ip = request.client.host if request.client else None

            async with admission:
                status, body, content_type = await run_in_threadpool(
                    _relay_report, raw_body, headers, source_ip, run_id)
            # txtv's response is returned verbatim so Vapi sees the real
            # outcome and its retry logic works against the truth.
            return Response(content=body, status_code=status, media_type=content_type)

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
