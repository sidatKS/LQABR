"""Observability — the four log streams, and the run correlation token.

v2 design, cross-cutting requirement. Four distinct logs split by layer,
each answering a different question when something needs investigating:

    system_log    Container activity. Is the service itself healthy?
                  Start / stop / restart of the runtime, independent of any
                  lead or run. The only stream meaningful with no run in
                  flight — it is not written by any single step.
    process_log   Agent activity, and every decision. Which step ran, when,
                  with what result — and the decisions the design names
                  explicitly: WHICH SKILL WAS CHOSEN (step 6), WHICH STATUS
                  WON (step 9), and WHY A RUN ENDED (terminal status).
    audit_log     Network activity: endpoint, direction, time. Every HTTP
                  request/response this system makes or receives — gateway
                  entry (2), token call (4), HubSpot GET (5), Mailgun send
                  (7), Mailgun event push (8), HubSpot POST/PATCH (9),
                  including which bearer was used, status codes and retries.
    model_log     Model activity. Input tokens, output tokens for the one
                  model call made per lead at step 6.

Every record in all four streams is keyed by ``object_id`` + ``run_id``,
so one run can be reassembled across every layer.

Never logged: the bearer value itself, the Mailgun API key, the webhook
signing key. `_redact()` scrubs any field whose name looks like a
credential, and `bearer_fingerprint()` gives audit_log a stable, non-
reversible way to say *which* bearer was used (step 4 / 5 / 9 requirement)
without ever writing the token down.

Sinks: JSON lines to a stdlib logger per stream (``lqabr.email.<stream>``),
which is what Cloud Run's structured logging picks up. Set
``LQABR_EMAIL_LOG_DIR`` to additionally tee each stream to
``<dir>/<stream>.jsonl`` for local inspection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SYSTEM = "system_log"
PROCESS = "process_log"
AUDIT = "audit_log"
MODEL = "model_log"

STREAMS = (SYSTEM, PROCESS, AUDIT, MODEL)

# Field names whose values are credentials and must never reach a log line.
_SECRET_FIELDS = frozenset({
    "token", "access_token", "bearer", "authorization", "api_key", "apikey",
    "secret", "client_secret", "signing_key", "password", "credential",
})
_REDACTED = "***redacted***"

_loggers: Dict[str, logging.Logger] = {}
_file_handles: Dict[str, Any] = {}
_lock = threading.Lock()


# --------------------------------------------------------------- run context
@dataclass(frozen=True)
class RunContext:
    """STEP 3 — the one key that must persist across HubSpot, this agent and
    Mailgun. Bound once when the gateway invocation is accepted, then carried
    by every later step, every log line in all four streams, and every
    outbound call."""

    object_id: str
    run_id: str

    @property
    def correlation_token(self) -> str:
        """The single string form sent to Mailgun and echoed back on events
        days later (step 7 -> step 8)."""
        return f"{self.object_id}:{self.run_id}"

    def as_key(self) -> Dict[str, str]:
        return {"object_id": self.object_id, "run_id": self.run_id}


def bind_run(object_id: str, run_id: Optional[str] = None) -> RunContext:
    """STEP 3 — start a run and bind its correlation token.

    `run_id` is generated unless the caller supplies one (replay/resume).
    Emits the process_log 'run started' record; from here on every record in
    every stream carries this token."""
    if not object_id:
        raise ValueError("object_id is required — a run cannot be correlated without it")
    ctx = RunContext(object_id=str(object_id), run_id=run_id or uuid.uuid4().hex)
    process(ctx, step=3, event="run_started",
            detail="object_id + run_id bound for the life of the run")
    return ctx


def parse_correlation_token(token: str) -> Optional[RunContext]:
    """Rebuild the RunContext from the token Mailgun echoed back (step 8).
    Returns None for anything that isn't a well-formed token, so a junk
    event is flagged rather than crashing the receiver."""
    if not token or ":" not in token:
        return None
    object_id, _, run_id = token.partition(":")
    if not object_id or not run_id:
        return None
    return RunContext(object_id=object_id, run_id=run_id)


def bearer_fingerprint(bearer: Optional[str]) -> str:
    """A stable, non-reversible 12-char id for a bearer token, so audit_log
    can record WHICH bearer was used on a HubSpot hop without ever holding
    the value. Different tokens fingerprint differently; the same token
    fingerprints the same across records, which is the whole point."""
    if not bearer:
        return "none"
    return hashlib.sha256(bearer.encode()).hexdigest()[:12]


# ------------------------------------------------------------------- writers
def _redact(value: Any, key: str = "") -> Any:
    if key.lower() in _SECRET_FIELDS:
        return _REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _logger(stream: str) -> logging.Logger:
    logger = _loggers.get(stream)
    if logger is None:
        logger = logging.getLogger(f"lqabr.email.{stream}")
        _loggers[stream] = logger
    return logger


def _tee_path(stream: str) -> Optional[Path]:
    directory = os.environ.get("LQABR_EMAIL_LOG_DIR")
    if not directory:
        return None
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{stream}.jsonl"


def emit(stream: str, ctx: Optional[RunContext], **fields: Any) -> Dict[str, Any]:
    """Write one record to one stream. Returns the record (handy in tests).

    `ctx` is optional only for system_log, which is the one stream that is
    meaningful when no run is in flight."""
    if stream not in STREAMS:
        raise ValueError(f"unknown log stream {stream!r}; expected one of {STREAMS}")

    record: Dict[str, Any] = {
        "stream": stream,
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": "email_agent",
        "object_id": ctx.object_id if ctx else None,
        "run_id": ctx.run_id if ctx else None,
    }
    record.update({k: _redact(v, k) for k, v in fields.items()})

    line = json.dumps(record, default=str, sort_keys=False)
    _logger(stream).info(line)

    path = _tee_path(stream)
    if path is not None:
        with _lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    return record


def system(**fields: Any) -> Dict[str, Any]:
    """Container activity — start / stop / restart, sandbox host. Runs
    continuously and is not tied to any step or run."""
    fields.setdefault("host", os.environ.get("K_REVISION") or os.environ.get("HOSTNAME", "local"))
    return emit(SYSTEM, None, **fields)


def process(ctx: Optional[RunContext], *, step: Optional[int] = None,
            event: str, **fields: Any) -> Dict[str, Any]:
    """Agent activity and every decision — which step ran, what it decided,
    and why a run ended."""
    return emit(PROCESS, ctx, step=step, event=event, **fields)


def audit(ctx: Optional[RunContext], *, step: Optional[int] = None,
          direction: str, endpoint: str, method: str = "",
          status_code: Optional[int] = None, bearer: Optional[str] = None,
          **fields: Any) -> Dict[str, Any]:
    """Network activity — endpoint, direction, time. `bearer` is fingerprinted,
    never written: pass the raw token and it is hashed here."""
    if direction not in ("outbound", "inbound"):
        raise ValueError("direction must be 'outbound' or 'inbound'")
    return emit(AUDIT, ctx, step=step, direction=direction, endpoint=endpoint,
                method=method, status_code=status_code,
                bearer_fingerprint=bearer_fingerprint(bearer), **fields)


def model(ctx: Optional[RunContext], *, step: int = 6, model_name: str,
          input_tokens: Optional[int] = None, output_tokens: Optional[int] = None,
          **fields: Any) -> Dict[str, Any]:
    """Model activity — one record per lead, because there is one model call
    per lead at step 6.

    Prompt/response content is written only when
    ``LQABR_EMAIL_LOG_MODEL_CONTENT=1``. It is off by default: the v2 doc
    flags that allowing content here would put prospect PII in the logs, and
    that question is not yet settled."""
    if not _bool_env("LQABR_EMAIL_LOG_MODEL_CONTENT"):
        fields.pop("prompt", None)
        fields.pop("completion", None)
        fields["content_logged"] = False
    else:
        fields["content_logged"] = True
    return emit(MODEL, ctx, step=step, model=model_name,
                input_tokens=input_tokens, output_tokens=output_tokens, **fields)


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------ MCP obs bridge
class MCPObservability:
    """Adapter handed to ``mcp.hubspot`` so the shared MCP can write into
    THIS agent's streams without importing anything agent-specific.

    The MCP folder is central — email, voice and scheduling all reference the
    same copy — so it must not depend on any one agent's logging. It accepts
    a sink with `.audit()`/`.process()` and defaults to a no-op; this is the
    email agent's implementation of that sink."""

    def __init__(self, ctx: Optional[RunContext] = None) -> None:
        self.ctx = ctx

    def bind(self, ctx: RunContext) -> "MCPObservability":
        self.ctx = ctx
        return self

    def audit(self, **fields: Any) -> None:
        audit(self.ctx, **fields)

    def process(self, **fields: Any) -> None:
        process(self.ctx, **fields)


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a plain stdout handler to the four stream loggers if the host
    hasn't configured logging. Cloud Run reads stdout, so one line per
    record is exactly what we want."""
    for stream in STREAMS:
        logger = _logger(stream)
        logger.setLevel(level)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        logger.propagate = False
