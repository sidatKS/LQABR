"""Structured logging for LQABR agents.

Every agent process writes three separate, correlated log streams so a
single lead's journey can be reconstructed across services from log
search alone:

    system   Technical/infra events -- exceptions, retries, startup,
             config problems. Plain `logging`, goes to stderr -> Cloud
             Logging like any other Python service log.
    process  One structured (JSON) line per tool call: which tool ran,
             for which lead, how long it took, and whether it succeeded.
             Applied automatically via the `@log_tool_call` decorator --
             every function passed to `Agent(tools=[...])` should carry it.
    audit    A durable, structured record of state-changing business
             actions only: a lead's stage/probability changed, an email/
             SMS/call went out, a dispatch decision was made. This is the
             trail reviewers/compliance can replay independently of any
             agent's uptime -- never sampled or dropped silently.

Usage in an agent module:

    from lqabr_core.logging import get_agent_logger, log_tool_call, correlation_scope

    log = get_agent_logger("email_agent")

    @log_tool_call(log, audit=True)
    def send_outreach_email(contact_email: str, ...) -> Dict[str, Any]:
        ...

    # Around a whole request / dispatch cycle, so every log line inside it
    # shares one trace id and lead id:
    with correlation_scope(hubspot_contact_id=lead.hubspot_contact_id):
        send_outreach_email(lead.email, ...)

No third-party dependency: stdlib `logging` + a small JSON formatter, so
every agent gets this for free with no new requirements.txt entry.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, TypeVar

_REDACT_SUBSTRINGS = ("token", "secret", "password", "auth", "api_key", "apikey")

_trace_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "lqabr_trace_id", default=None)
_contact_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "lqabr_contact_id", default=None)


class _JsonFormatter(logging.Formatter):
    """Renders each log record as one JSON line with common correlation
    fields, so system/process/audit logs are all machine-parseable and
    joinable by trace_id / hubspot_contact_id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = _trace_id_var.get()
        contact_id = _contact_id_var.get()
        if trace_id:
            payload["trace_id"] = trace_id
        if contact_id:
            payload["hubspot_contact_id"] = contact_id
        extra = getattr(record, "lqabr_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _configure(logger: logging.Logger) -> logging.Logger:
    if logger.handlers:
        return logger
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("LQABR_LOG_LEVEL", "INFO"))
    logger.propagate = False
    return logger


def get_system_logger(agent_name: str) -> logging.Logger:
    """Technical/infra logger -- exceptions, retries, startup."""
    return _configure(logging.getLogger(f"lqabr.{agent_name}.system"))


def get_process_logger(agent_name: str) -> logging.Logger:
    """Per-tool-call trace logger -- one line per invocation."""
    return _configure(logging.getLogger(f"lqabr.{agent_name}.process"))


def get_audit_logger(agent_name: str) -> logging.Logger:
    """Durable business-action logger -- stage/probability changes, sends,
    dispatch decisions."""
    return _configure(logging.getLogger(f"lqabr.{agent_name}.audit"))


class AgentLoggers:
    """Bundle of the three loggers for one agent, handed to `log_tool_call`."""

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self.system = get_system_logger(agent_name)
        self.process = get_process_logger(agent_name)
        self.audit = get_audit_logger(agent_name)


def get_agent_logger(agent_name: str) -> AgentLoggers:
    return AgentLoggers(agent_name)


def correlation_scope(trace_id: Optional[str] = None,
                      hubspot_contact_id: Optional[str] = None) -> "_Scope":
    """Context manager: tag every log line emitted inside the block with
    the same trace id (auto-generated if omitted) and lead id, so a
    dispatch cycle or webhook request can be reconstructed end-to-end."""
    return _Scope(trace_id, hubspot_contact_id)


class _Scope:
    def __init__(self, trace_id: Optional[str], hubspot_contact_id: Optional[str]) -> None:
        self._trace_id = trace_id or str(uuid.uuid4())
        self._contact_id = hubspot_contact_id

    def __enter__(self) -> "_Scope":
        self._trace_token = _trace_id_var.set(self._trace_id)
        self._contact_token = _contact_id_var.set(self._contact_id)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        _trace_id_var.reset(self._trace_token)
        _contact_id_var.reset(self._contact_token)


def _redact(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: ("<redacted>" if any(s in k.lower() for s in _REDACT_SUBSTRINGS) else v)
        for k, v in kwargs.items()
    }


F = TypeVar("F", bound=Callable[..., Any])


def log_tool_call(loggers: AgentLoggers, audit: bool = False,
                  summarize: Optional[Callable[[Any], str]] = None) -> Callable[[F], F]:
    """Decorator for every function passed to `Agent(tools=[...])`.

    Always emits a process-log line (tool name, redacted args, duration,
    outcome). If `audit=True`, also emits an audit-log line -- use this on
    tools that change lead state or send real outreach (email/SMS/call/
    schedule/dispatch), not on read-only queries like `pipeline_status`.

    `summarize` optionally turns the tool's return value into a short
    human string for the audit line (defaults to the raw return value).
    Exceptions are logged to the system logger with a stack trace and
    always re-raised -- a tool failure is never swallowed here.
    """

    def decorator(func: F) -> F:
        sig = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tool_name = func.__name__
            started = time.monotonic()
            try:
                bound = sig.bind_partial(*args, **kwargs).arguments
            except TypeError:
                bound = kwargs  # fall back rather than fail the call over logging
            safe_kwargs = _redact(bound)
            loggers.process.info("tool call started", extra={
                "lqabr_fields": {"log_type": "process", "tool": tool_name,
                                "args": safe_kwargs, "event": "start"}})
            try:
                result = func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- log then always re-raise
                duration_ms = round((time.monotonic() - started) * 1000, 1)
                loggers.system.error("tool call failed: %s", exc, exc_info=True, extra={
                    "lqabr_fields": {"log_type": "system", "tool": tool_name,
                                    "outcome": "error", "duration_ms": duration_ms}})
                loggers.process.info("tool call finished", extra={
                    "lqabr_fields": {"log_type": "process", "tool": tool_name,
                                    "outcome": "error", "duration_ms": duration_ms}})
                raise
            duration_ms = round((time.monotonic() - started) * 1000, 1)
            loggers.process.info("tool call finished", extra={
                "lqabr_fields": {"log_type": "process", "tool": tool_name,
                                "outcome": "ok", "duration_ms": duration_ms}})
            if audit:
                summary = summarize(result) if summarize else result
                loggers.audit.info("business action", extra={
                    "lqabr_fields": {"log_type": "audit", "tool": tool_name,
                                    "outcome": "ok", "result": summary}})
            return result
        return wrapper  # type: ignore[return-value]
    return decorator
