"""Observability — structured, correlated, and free of secrets.

Three streams, one run_id correlating them:

    process   what the agent did and why (steps, decisions, counts)
    audit     every hop that left this process (HTTP fetch, MCP call),
              with endpoint, method, status and duration — never a payload
              and never a credential
    system    coarse resource snapshots, when psutil is installed

Records are one JSON object per line on stdout, which is what Cloud Logging
ingests. `redact()` is applied to every field bag on the way out, so a token
that reaches a log call by accident still does not reach the log.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

_SENSITIVE_HINTS = ("token", "secret", "password", "api_key", "apikey",
                    "authorization", "auth", "bearer", "credential")

_MAX_VALUE_CHARS = 500


def new_run_id() -> str:
    return f"sum-{uuid.uuid4().hex[:12]}"


def redact(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Blank anything whose NAME suggests a credential, and trim long values.

    Name-based, not value-based, on purpose: we cannot recognise a token by
    looking at it, but we always know what we called it.
    """
    clean: Dict[str, Any] = {}
    for key, value in fields.items():
        lowered = str(key).lower()
        if any(hint in lowered for hint in _SENSITIVE_HINTS):
            clean[key] = "<redacted>" if value else ""
            continue
        if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
            clean[key] = value[:_MAX_VALUE_CHARS] + f"…(+{len(value) - _MAX_VALUE_CHARS} chars)"
        else:
            clean[key] = value
    return clean


class _Stream:
    def __init__(self, name: str, run_id: str, logger: logging.Logger) -> None:
        self._name = name
        self._run_id = run_id
        self._logger = logger

    def emit(self, event: str, **fields: Any) -> Dict[str, Any]:
        record = {
            "stream": self._name,
            "run_id": self._run_id,
            "event": event,
            "ts": time.time(),
            **redact(fields),
        }
        self._logger.info(json.dumps(record, default=repr, ensure_ascii=False))
        return record


@dataclass
class Observability:
    """One run's logging handle. Build one per run; pass it down."""

    run_id: str = field(default_factory=new_run_id)
    logger: logging.Logger = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.logger is None:
            self.logger = logging.getLogger("lqabr.summary")
        self.process = _Stream("process", self.run_id, self.logger)
        self.audit = _Stream("audit", self.run_id, self.logger)
        self.system = _Stream("system", self.run_id, self.logger)

    # ------------------------------------------------------------------
    def hop(self, *, service: str, endpoint: str, method: str,
            status: Optional[int] = None, duration_ms: Optional[float] = None,
            attempt: int = 1, error: str = "") -> None:
        """One outbound call, as the audit trail records it. No bodies."""
        self.audit.emit(
            "outbound_call", service=service, endpoint=endpoint, method=method,
            status=status, duration_ms=duration_ms, attempt=attempt, error=error,
        )

    def snapshot(self, label: str, **fields: Any) -> None:
        try:
            import psutil  # type: ignore

            process = psutil.Process(os.getpid())
            fields["rss_mb"] = round(process.memory_info().rss / 1_048_576, 1)
            fields["cpu_percent"] = process.cpu_percent(interval=None)
        except Exception:  # psutil absent or unavailable — degrade, never fail
            fields.setdefault("psutil", "unavailable")
        self.system.emit(label, **fields)


_OBS: Observability | None = None


def configure_logging(level: str = "INFO", log_file: str = "") -> None:
    """JSON lines to stdout (Cloud Logging) and, when log_file is set, also to
    that file — the agent writes its own log, no shell redirect needed. The
    file handler is created once; the parent directory is made if missing."""
    root = logging.getLogger("lqabr.summary")
    fmt = logging.Formatter("%(message)s")
    if not root.handlers:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)
        root.propagate = False
    # add the file handler once (idempotent across repeated configure_logging calls)
    if log_file and not any(isinstance(h, logging.FileHandler)
                            and getattr(h, "_lqabr_path", "") == log_file
                            for h in root.handlers):
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            fh._lqabr_path = log_file  # type: ignore[attr-defined]
            root.addHandler(fh)
        except OSError:
            root.warning(json.dumps({"stream": "system", "event": "log_file_unavailable",
                                     "path": log_file}))
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_obs(run_id: str | None = None, *, refresh: bool = False) -> Observability:
    global _OBS
    if _OBS is None or refresh:
        _OBS = Observability(run_id=run_id or new_run_id())
    return _OBS
