"""Three log streams, split by layer — the Rev 5 observability contract.

Rev 5 asks a different question of each stream, so they are three distinct
loggers rather than one log with a level field:

    system_log   Container activity. The runtime itself, independent of any
                 lead or request: start/stop/restart, scaling, deploy/version
                 changes. Answers "is the service healthy" before you look at
                 any single call. Always on; not step-specific.
    process_log  Agent activity. Which step ran, when, with what result — and
                 because this agent is LLM-driven, its model activity too:
                 model invoked, input/output tokens, latency. Answers "where
                 did this call get stuck" and "what did the model decide, and
                 at what cost".
    audit_log    Network activity. Every HTTP request/response this system
                 makes or receives, including which credential was used
                 (never its value), status code, retry count and auth
                 failures. Answers "did this request actually go out, and what
                 came back".

Everything is emitted as one JSON object per line on stdout, which is what
Cloud Run's logging agent ingests: `severity` and `message` are promoted to
the LogEntry, every other key lands in `jsonPayload` and is queryable. So
`jsonPayload.log_stream="audit" AND jsonPayload.status_code>=400` is a Logs
Explorer filter, not a grep.

Correlation
-----------
One inbound lead produces log lines across four steps, two processes' worth of
callbacks and an unknown amount of wall-clock time (the call itself). A
`correlation_id` is bound once, at the gateway, and every line for that lead
carries it — plus `employee_id` and, once known, `call_id`. Without this,
reading the logs of two concurrent calls interleaved is guesswork.

Bound via contextvars, so the values follow the request through async handlers
and background tasks without being threaded through every signature.

Never log a credential value. `credential=` takes a *reference* — the Secret
Manager name or a token fingerprint — and `redact()` is available for the
cases where a payload has to be logged at all.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

# ---------------------------------------------------------------- streams

SYSTEM = "system"
PROCESS = "process"
AUDIT = "audit"

system_log = logging.getLogger("lqabr.system")
process_log = logging.getLogger("lqabr.process")
audit_log = logging.getLogger("lqabr.audit")

_STREAMS = {SYSTEM: system_log, PROCESS: process_log, AUDIT: audit_log}

# ------------------------------------------------------------ correlation

_correlation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "lqabr_correlation_id", default=None)
_context: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "lqabr_log_context", default={})


def new_correlation_id() -> str:
    """A fresh id for one lead's journey through steps 2-4 and 6-8."""
    return uuid.uuid4().hex[:16]


def bind(correlation_id: Optional[str] = None, **fields: Any) -> str:
    """Attach a correlation id and any always-log fields to this context.

    Called once per inbound request at the gateway. Returns the id in use so
    the caller can echo it back to the sender for cross-system tracing.
    """
    cid = correlation_id or _correlation_id.get() or new_correlation_id()
    _correlation_id.set(cid)
    merged = dict(_context.get())
    merged.update({k: v for k, v in fields.items() if v is not None})
    _context.set(merged)
    return cid


def current_correlation_id() -> Optional[str]:
    return _correlation_id.get()


@contextmanager
def correlation(correlation_id: Optional[str] = None, **fields: Any) -> Iterator[str]:
    """Scoped `bind` — restores the previous context on exit.

    Used by background tasks so one lead's bound fields never leak into the
    next request handled by the same worker.
    """
    cid_token = _correlation_id.set(correlation_id or new_correlation_id())
    merged = dict(_context.get())
    merged.update({k: v for k, v in fields.items() if v is not None})
    ctx_token = _context.set(merged)
    try:
        yield _correlation_id.get()  # type: ignore[arg-type]
    finally:
        _correlation_id.reset(cid_token)
        _context.reset(ctx_token)


# --------------------------------------------------------------- plumbing

class _JsonFormatter(logging.Formatter):
    """One JSON object per line, shaped for Cloud Run structured logging.

    `severity` and `message` are the two keys Cloud Run promotes onto the
    LogEntry itself; everything else is queryable under `jsonPayload`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "log_stream": getattr(record, "log_stream", record.name.rsplit(".", 1)[-1]),
            "logger": record.name,
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        extra = getattr(record, "lqabr", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


_configured = False


def configure(level: Optional[str] = None, json_output: Optional[bool] = None) -> None:
    """Attach handlers to the three streams. Idempotent — safe to call from
    every module's import.

    Why explicit handlers rather than relying on the root logger: `uvicorn
    webhook_app:app` installs handlers on its own loggers and leaves the root
    logger at WARNING, so anything we log at INFO propagates to a root that
    drops it. The request shows up in the access log and the diagnostic next
    to it does not — which is how a live call ends up debuggable only from the
    provider's console. Own the handler and INFO survives however the app is
    launched.

    LQABR_LOG_LEVEL  overrides the level (default INFO).
    LQABR_LOG_JSON=0 switches to a human-readable formatter for local dev.
    """
    global _configured
    if _configured:
        return

    resolved_level = (level or os.environ.get("LQABR_LOG_LEVEL", "INFO")).upper()
    if json_output is None:
        json_output = os.environ.get("LQABR_LOG_JSON", "1") != "0"

    formatter: logging.Formatter
    if json_output:
        formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s %(message)s", "%H:%M:%S")

    for logger in _STREAMS.values():
        logger.setLevel(resolved_level)
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        # Do not also hand these to the root logger: Cloud Run would ingest
        # every line twice, once structured and once not.
        logger.propagate = False

    _configured = True


def _emit(stream: str, level: int, message: str, fields: Dict[str, Any],
          exc_info: bool = False) -> None:
    configure()
    payload = dict(_context.get())
    cid = _correlation_id.get()
    if cid:
        payload["correlation_id"] = cid
    payload.update({k: v for k, v in fields.items() if v is not None})
    payload["log_stream"] = stream
    _STREAMS[stream].log(level, message, exc_info=exc_info,
                         extra={"lqabr": payload, "log_stream": stream})


def redact(value: Optional[str], keep: int = 4) -> str:
    """Fingerprint a credential for logs: length plus its last few characters.

    Enough to tell two tokens apart in a log line, useless to anyone who
    reads it. Secrets are never logged in full, anywhere.
    """
    if not value:
        return "<unset>"
    tail = value[-keep:] if len(value) > keep else ""
    return f"<len={len(value)} ...{tail}>"


# ------------------------------------------------------------- system_log

def log_system(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Container-level activity: start/stop, config resolution, scaling.

    Deliberately carries no lead or request identity — this stream answers
    "is the service itself healthy", and mixing per-call detail into it
    defeats that.
    """
    configure()
    payload = {k: v for k, v in fields.items() if v is not None}
    payload["log_stream"] = SYSTEM
    system_log.log(level, event, extra={"lqabr": payload, "log_stream": SYSTEM})


def log_startup(service: str, **fields: Any) -> None:
    log_system("service.start", service=service,
               revision=os.environ.get("K_REVISION"),
               gcp_project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
               python=sys.version.split()[0], **fields)


def log_shutdown(service: str, **fields: Any) -> None:
    log_system("service.stop", service=service,
               revision=os.environ.get("K_REVISION"), **fields)


# ------------------------------------------------------------ process_log

# Canonical step names, so a Logs Explorer filter on one step is exact rather
# than a substring match on free text. These mirror the Rev 5 step numbers.
STEP_GATEWAY_LEAD = "step2.gateway.hubspot_lead"
STEP_READ_LEAD = "step3.read_lead"
STEP_PLACE_CALL = "step4.place_call"
STEP_GATEWAY_REPORT = "step6to7.gateway.vapi_report"
STEP_SUMMARISE = "step7.summarise_report"
STEP_PUSH_MCP = "step8.push_to_mcp"


def log_process(step: str, status: str, message: Optional[str] = None,
                level: int = logging.INFO, **fields: Any) -> None:
    """One step of one call: which step, what result.

    `status` is a small vocabulary on purpose — started / ok / stopped /
    error / skipped — so "where did this call get stuck" is a query for the
    last line of a correlation_id, not a reading exercise.
    """
    _emit(PROCESS, level, message or f"{step} {status}",
          {"step": step, "status": status, **fields},
          exc_info=(level >= logging.ERROR))


def log_model_call(step: str, model: str, latency_ms: float,
                   input_tokens: Optional[int] = None,
                   output_tokens: Optional[int] = None,
                   outcome: Optional[str] = None, **fields: Any) -> None:
    """Model activity for an LLM-driven step — Rev 5 names Step 7 explicitly.

    Tokens and latency are the cost and the tail-latency story for this
    service; without them "what did the model decide, and at what cost" is
    unanswerable after the fact.
    """
    _emit(PROCESS, logging.INFO, f"{step} model.invoked",
          {"step": step, "status": "model", "model": model,
           "latency_ms": round(latency_ms, 1), "input_tokens": input_tokens,
           "output_tokens": output_tokens,
           "total_tokens": (None if input_tokens is None or output_tokens is None
                            else input_tokens + output_tokens),
           "outcome": outcome, **fields})


@contextmanager
def step(step_name: str, **fields: Any) -> Iterator[Dict[str, Any]]:
    """Bracket a step with started/ok/error lines and a measured duration.

    Anything the body puts into the yielded dict is logged on the closing
    line, so a step's result travels with its timing.
    """
    log_process(step_name, "started", **fields)
    result: Dict[str, Any] = {}
    started = time.perf_counter()
    try:
        yield result
    except Exception as exc:  # noqa: BLE001 - re-raised below
        log_process(step_name, "error", f"{step_name} raised {type(exc).__name__}",
                    level=logging.ERROR,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    error=str(exc), **fields)
        raise
    log_process(step_name, result.pop("status", "ok"),
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                **{**fields, **result})


# -------------------------------------------------------------- audit_log

def log_http_out(method: str, url: str, status_code: Optional[int] = None,
                 credential: Optional[str] = None, attempt: int = 1,
                 duration_ms: Optional[float] = None,
                 error: Optional[str] = None, **fields: Any) -> None:
    """An outbound request this system made — Steps 3, 4 and 8.

    `credential` is a *reference* (Secret Manager name or fingerprint), never
    a value. `attempt` is what makes a retried-then-succeeded call
    distinguishable from a clean one.
    """
    level = logging.INFO
    if error or (status_code is not None and status_code >= 500):
        level = logging.ERROR
    elif status_code is not None and status_code >= 400:
        level = logging.WARNING
    _emit(AUDIT, level, f"-> {method} {_safe_url(url)} {status_code or 'no-response'}",
          {"direction": "outbound", "method": method, "url": _safe_url(url),
           "status_code": status_code, "credential": credential,
           "attempt": attempt,
           "duration_ms": None if duration_ms is None else round(duration_ms, 1),
           "error": error, **fields})


def log_http_in(method: str, path: str, status_code: int,
                signature_check: Optional[str] = None,
                credential: Optional[str] = None,
                error: Optional[str] = None, **fields: Any) -> None:
    """An inbound request this system received — Step 2 and Step 6->7.

    `signature_check` records how authenticity was established (or that it
    was skipped), which is the first thing anyone asks when a webhook did
    something unexpected.
    """
    level = logging.INFO
    if status_code >= 500:
        level = logging.ERROR
    elif status_code >= 400:
        level = logging.WARNING
    _emit(AUDIT, level, f"<- {method} {path} {status_code}",
          {"direction": "inbound", "method": method, "path": path,
           "status_code": status_code, "signature_check": signature_check,
           "credential": credential, "error": error, **fields})


def _safe_url(url: str) -> str:
    """Strip the query string from a logged URL.

    Callback URLs in this system carry identifiers, and provider URLs can
    carry tokens; the path is what makes a log line useful and the query
    string is what makes it a liability.
    """
    return url.split("?", 1)[0]


__all__ = [
    "SYSTEM", "PROCESS", "AUDIT",
    "system_log", "process_log", "audit_log",
    "configure", "bind", "correlation", "new_correlation_id",
    "current_correlation_id", "redact",
    "log_system", "log_startup", "log_shutdown",
    "log_process", "log_model_call", "step",
    "log_http_out", "log_http_in",
    "STEP_GATEWAY_LEAD", "STEP_READ_LEAD", "STEP_PLACE_CALL",
    "STEP_GATEWAY_REPORT", "STEP_SUMMARISE", "STEP_PUSH_MCP",
]
