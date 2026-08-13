"""The four logs.

FR "Observability — four logs, required per step":

  system   container activity: memory, CPU, resource use
  process  agent activity & DECISIONS — what the step did and *why*
  audit    network activity: every external (HubSpot) call — endpoint, status, timing
  tokens   input/output tokens + model activity, wherever a model runs

Every line is JSON and carries run_id; lines that concern one lead also carry
lead_ref_id.

This agent is deterministic — NO model — so the tokens stream is wired but
emits nothing (``enabled=False``). It exists because the shared obs module is
reused by the model-using agents (email / voice), which DO have tokens to
record. See the Planner card note: "no model = no token log".

Loggers live in this shared module (lqabr_core/obs/), NOT under the agent,
because every agent in the platform emits the same four streams.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from weakref import WeakValueDictionary

from .context import RunContext, get_current_run, utc_now_iso

LOG_TYPES = ("system", "process", "audit", "tokens")

_FILE_SINKS: dict[str, TextIO] = {}
_FILE_LOCK = threading.Lock()


def _log_to_file_enabled() -> bool:
    return os.getenv("LQABR_LOG_TO_FILE", "false").strip().lower() in {"1", "true", "yes"}


def _file_sink(log_type: str) -> TextIO | None:
    """Optional per-stream file sink. Off by default; Cloud Run reads stdout."""
    if not _log_to_file_enabled():
        return None
    with _FILE_LOCK:
        sink = _FILE_SINKS.get(log_type)
        if sink is None:
            log_dir = Path(os.getenv("LQABR_LOG_DIR", "logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            sink = (log_dir / f"{log_type}.jsonl").open("a", encoding="utf-8")
            _FILE_SINKS[log_type] = sink
        return sink


def close_file_sinks() -> None:
    with _FILE_LOCK:
        for sink in _FILE_SINKS.values():
            try:
                sink.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                pass
        _FILE_SINKS.clear()


@dataclass
class LogStream:
    """One of the four streams."""

    log_type: str
    run_ctx: RunContext
    enabled: bool = True
    stream: TextIO | None = None

    def emit(self, event: str, lead_ref_id: str | None = None, **fields: Any) -> dict[str, Any] | None:
        """Write one structured line. Returns the record (or None if disabled).

        Never raises: observability must not be able to kill a run.
        """
        if not self.enabled:
            return None

        record: dict[str, Any] = {
            "ts": utc_now_iso(),
            "log": self.log_type,
            "run_id": self.run_ctx.run_id,
            "agent": self.run_ctx.agent,
            "event": event,
        }
        if lead_ref_id:
            record["lead_ref_id"] = lead_ref_id
        for key, value in fields.items():
            if value is not None:
                record[key] = value

        try:
            line = json.dumps(record, default=str, ensure_ascii=False)
        except Exception:
            line = json.dumps({**{k: str(v) for k, v in record.items()}}, ensure_ascii=False)

        out = self.stream or sys.stdout
        try:
            out.write(line + "\n")
            out.flush()
        except Exception:  # pragma: no cover
            pass

        sink = _file_sink(self.log_type)
        if sink is not None:
            try:
                sink.write(line + "\n")
                sink.flush()
            except Exception:  # pragma: no cover
                pass

        return record


class _Timer:
    """Context manager that measures a block and emits duration_ms."""

    def __init__(self, stream: LogStream, event: str, lead_ref_id: str | None, fields: dict[str, Any]):
        self._stream = stream
        self._event = event
        self._lead_ref_id = lead_ref_id
        self._fields = fields
        self._start = 0.0
        self.extra: dict[str, Any] = {}

    def __enter__(self) -> "_Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration_ms = round((time.perf_counter() - self._start) * 1000, 2)
        fields = {**self._fields, **self.extra, "duration_ms": duration_ms}
        if exc is not None:
            fields["outcome"] = "error"
            fields["error"] = f"{exc_type.__name__}: {exc}"
        else:
            fields.setdefault("outcome", "ok")
        self._stream.emit(self._event, lead_ref_id=self._lead_ref_id, **fields)
        return False  # never swallow


class Observability:
    """The four streams, bound to one run.

    ``tokens_enabled`` defaults to the run context's ``uses_model`` flag. When
    the lead_profile agent ran as a pure pipeline this was always False and the
    stream was wired-but-silent. Under ADK a model orchestrates the steps, so
    the fourth log becomes real for this agent too — the FR's "tokens/model
    log, wherever a model runs" now applies here.
    """

    def __init__(
        self,
        run_ctx: RunContext,
        tokens_enabled: bool | None = None,
        stream: TextIO | None = None,
    ):
        self.run_ctx = run_ctx
        self.system = LogStream("system", run_ctx, stream=stream)
        self.process = LogStream("process", run_ctx, stream=stream)
        self.audit = LogStream("audit", run_ctx, stream=stream)
        if tokens_enabled is None:
            tokens_enabled = getattr(run_ctx, "uses_model", False)
        self.tokens = LogStream("tokens", run_ctx, enabled=tokens_enabled, stream=stream)

    @property
    def run_id(self) -> str:
        return self.run_ctx.run_id

    def timed_audit(self, event: str, lead_ref_id: str | None = None, **fields: Any) -> _Timer:
        """Time an external call and emit one audit line with duration_ms."""
        return _Timer(self.audit, event, lead_ref_id, fields)

    def system_snapshot(self, event: str, **fields: Any) -> dict[str, Any] | None:
        """Emit container resource use: memory, CPU."""
        usage: dict[str, Any] = {}
        try:
            import psutil  # imported lazily so tests run without it

            proc = psutil.Process()
            with proc.oneshot():
                usage["rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 2)
                usage["cpu_percent"] = proc.cpu_percent(interval=None)
            usage["system_memory_percent"] = psutil.virtual_memory().percent
        except Exception:
            try:
                import resource

                usage["max_rss_mb"] = round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2
                )
            except Exception:
                usage["resource_probe"] = "unavailable"
        return self.system.emit(event, **usage, **fields)


_CURRENT_OBS: ContextVar[Observability | None] = ContextVar("lqabr_obs", default=None)
_OBS_BY_RUN: "WeakValueDictionary[str, Observability]" = WeakValueDictionary()
_OBS_KEEPALIVE: dict[str, Observability] = {}


def get_obs(run_ctx: RunContext | None = None) -> Observability:
    """The Observability bundle for the CURRENT run.

    Review finding B10. This used to be a plain module global while
    ``RunContext`` was a ContextVar, so a second agent sharing the process —
    which is precisely what the in-process MCP design intends — had its HubSpot
    calls logged under the FIRST agent's run_id and agent name.

    Resolution order:
      1. an explicitly passed run context
      2. the Observability bound to the current context (set_obs)
      3. the bundle already created for the current RunContext
      4. a fresh bundle for the current RunContext

    The in-process MCP tools call this with no argument, so a tool invoked by
    any agent now genuinely logs under *that* agent's run.
    """
    ctx = run_ctx or get_current_run()

    bound = _CURRENT_OBS.get()
    if bound is not None and bound.run_ctx.run_id == ctx.run_id:
        return bound

    existing = _OBS_BY_RUN.get(ctx.run_id)
    if existing is not None:
        return existing

    obs = Observability(ctx)
    _remember(obs)
    return obs


def _remember(obs: Observability) -> None:
    _OBS_BY_RUN[obs.run_ctx.run_id] = obs
    _OBS_KEEPALIVE[obs.run_ctx.run_id] = obs


def set_obs(obs: Observability) -> Observability:
    """Bind an Observability bundle to the current execution context."""
    _CURRENT_OBS.set(obs)
    _remember(obs)
    return obs


def reset_obs() -> None:
    _CURRENT_OBS.set(None)
    _OBS_BY_RUN.clear()
    _OBS_KEEPALIVE.clear()
