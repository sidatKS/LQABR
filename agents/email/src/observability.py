"""Observability for the email agent — the four log streams, both renderers,
and the handler setup.

    system_log   container start/stop
    process_log  steps, spans, per-lead progress
    audit_log    every boundary hop, bearer fingerprinted
    model_log    tokens, durations, prompt/completion

Each event is built ONCE and rendered twice: an aligned line on stdout for
whoever is watching, and one JSON object per line on disk, split one file per
stream under ``logs/agents/email``.

THE ONE OWNER of the ``lqabr.email`` logger. outreach.py, events.py,
service_app.py and email_agent.py import from here and call
``configure_logging()``; none of them installs a handler. Three modules each
guarding their own setup with ``if not handlers`` is how a mode or a
destination silently gets discarded by whichever import happens to win.

Nothing here may import agent code — the MCP at the project root is handed
``MCPObservability`` precisely because mcp/hubspot/ cannot import from an
agent, and this module has to stay equally free of back-references.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import textwrap
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

#: Repo root — this file is agents/email/src/observability.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ logging
# Four streams (system / process / audit / model). Each event is built ONCE and
# rendered twice: an aligned line on stdout for whoever is watching, and one
# JSON object per line in the log file for grep and replay. Every line is
# stamped objectId + run_id so a whole run greps back together.
# ``step=`` values are join keys shared with the central MCP — do not renumber.
#
# ONE OWNER. service_app.py and email_agent.py call configure_logging() here;
# they must never install handlers themselves. Three copies racing on
# "if not handlers" is how a mode or destination silently gets discarded.
AGENT_NAME = "email_agent"
_LOG = logging.getLogger("lqabr.email")

LOG_MODES = ("normal", "debug")
LOG_FORMATS = ("console", "json", "auto")
#: One file per stream. The console still shows every stream interleaved —
#: only the stored copy is split, so each file reads as one concern.
LOG_FILES = {
    "process_log": "email_process.log",
    "audit_log": "email_audit.log",
    "system_log": "email_system.log",
    "model_log": "email_model.log",
}
LOG_FILE_NAME = LOG_FILES["process_log"]  # kept: the file callers name by default


def log_mode() -> str:
    """normal | debug. Debug raises the level, keeps empty fields, and stops
    truncating values — it is the only switch (LQABR_EMAIL_LOG_MODEL_CONTENT
    is folded into it)."""
    mode = os.environ.get("LQABR_EMAIL_LOG_MODE", "normal").strip().lower() or "normal"
    if mode not in LOG_MODES:
        raise RuntimeError(
            f"LQABR_EMAIL_LOG_MODE={mode!r} is not one of {'|'.join(LOG_MODES)}")
    return mode


def debug_mode() -> bool:
    return log_mode() == "debug"


def _max_field_chars() -> int:
    """Console only — the JSON file always stores the full value.

    DEBUG PRINTS EVERYTHING (0 = no cap): a 20,000-character prompt is exactly
    what debug mode exists to show. It stays readable because the console
    wraps long values with a hanging indent, so continuation lines sit under
    the value column instead of falling back to column 0 and burying the
    timestamps. Normal mode caps, to keep one event to one glance."""
    raw = os.environ.get("LQABR_EMAIL_LOG_MAX_FIELD_CHARS", "").strip()
    if raw:
        return max(0, int(raw))
    return 0 if debug_mode() else 160


def _terminal_width(default: int = 120) -> int:
    raw = os.environ.get("LQABR_EMAIL_LOG_WIDTH", "").strip()
    if raw:
        return max(60, int(raw))
    try:
        return max(60, shutil.get_terminal_size((default, 24)).columns)
    except Exception:  # noqa: BLE001 — a width is never worth failing a log for
        return default


def preview(value: Any, limit: Optional[int] = None) -> str:
    """Head of a long value plus a count of what was dropped."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    cap = _max_field_chars() if limit is None else limit
    if not cap or len(text) <= cap:
        return text
    return f"{text[:cap]}… (+{len(text) - cap} chars)"


def fields(obj: Any) -> Dict[str, Any]:
    """Every dataclass field, empties included. Driven off
    ``__dataclass_fields__`` so a field added to ValidatedProfile later logs
    itself instead of being silently missed."""
    names = getattr(obj, "__dataclass_fields__", None)
    return {name: getattr(obj, name, None) for name in names} if names else {}


# Glyph vocabulary, mirrored from the research agent's console.
IN, OUT, FAIL, SUB, HOP, OK, BUSY, START = "▸ IN", "◂ OUT", "✗ OUT", "+", "←", "✓", "⋯", "•"
_COLOUR = {START: "36", IN: "36", OUT: "32", FAIL: "31", SUB: "33",
           HOP: "35", OK: "32", BUSY: "90"}
#: Rendered in the head of the console line, so never repeated in the pairs.
_HEAD_KEYS = {"stream", "ts", "agent", "event", "outcome", "duration_ms", "objectId"}
#: Never in the console. Constant for the run (run_id), a join key for the JSON
#: rather than for a reader (step), or already in the head.
#:
#: `object_id` is here for the SHARED package only. The email agent writes
#: `objectId` everywhere — it is the head key, and the inbound trigger field.
#: mcp/hubspot still passes `object_id=` on its own obs calls (it is the
#: ValidatedProfile attribute name, which this agent does not own and must not
#: rename), and those records are adopted into our streams. Without this the
#: same id would print twice on one line, once per spelling.
#: Every one of them is still written to the file, which is what greps by them.
_CONSOLE_QUIET = {"run_id", "step", "object_id"}
#: Under this many leaves, all short, an event stays on one line. Exploding
#: `state: working` onto its own row is what makes detail feel like noise.
_INLINE_MAX_FIELDS = 2
_INLINE_MAX_CHARS = 46


def _expand(key: str, value: Any) -> Iterator[Tuple[str, str]]:
    """Explode one field into one line per leaf.

    A list becomes ``leads[0]``, ``leads[1]``…; a dict becomes
    ``arguments.objectId`` — so every nested value carries its own path and is
    greppable on its own. This is what makes debug output readable without
    concatenating anything into a sentence."""
    if isinstance(value, dict):
        if not value:
            yield key, "{}"
        for sub, item in value.items():
            yield from _expand(f"{key}.{sub}", item)
    elif isinstance(value, (list, tuple)):
        if not value:
            yield key, "[]"
        for index, item in enumerate(value):
            yield from _expand(f"{key}[{index}]", item)
    else:
        yield key, _flat(value)


def _flat(value: Any) -> str:
    """One field value, one line. Never built by concatenation into prose."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        text = "[" + ",".join(str(v) for v in value) + "]"
    elif isinstance(value, dict):
        text = json.dumps(value, default=str, sort_keys=True)
    else:
        text = str(value)
    return preview(text.replace("\n", " ").strip())


class JsonFormatter(logging.Formatter):
    """One JSON object per line — what the log file stores."""

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "lqabr", None)
        if payload is None:  # a stray record on a child logger
            return json.dumps({"stream": "system_log", "agent": AGENT_NAME,
                               "ts": datetime.now(timezone.utc).isoformat(),
                               "message": record.getMessage()}, default=str)
        return json.dumps(payload, default=str)


#: lqabr_core's loggers ADOPTED into our handlers, so a line the SHARED package
#: writes lands in the same file, in the same shape, as one the agent writes.
#: Left alone, a HubSpot call made by lqabr_core prints as a raw JSON blob
#: mid-console while the same call made here prints as a formatted hop — the
#: same request rendered two different ways, which is why a HubSpot GET looked
#: "missing" from our audit stream.
#:
#: FOUR STREAMS, FOUR LOGGERS — this map is exactly ``LOG_FILES`` and nothing
#: else. name -> the stream its records belong to.
#:
#:   lqabr.model  IS adopted, and was not before: `lqabr_core.model` logs which
#:                credential a model resolved through a plain logger of its own,
#:                so those lines went to the root logger — which uvicorn leaves
#:                at WARNING — and were never seen.
#:
#: DELIBERATELY NOT ADOPTED:
#:   lqabr.secrets  credential resolution. Not one of the four streams.
#:   uvicorn.*      the HTTP record of a request is infrastructure, not one of
#:                  our semantic streams. Uvicorn keeps its own handlers and
#:                  prints its own access line (`INFO: 1.2.3.4:0 - "POST
#:                  /email/campaign" 200 OK`); it does not reach the log files.
_ADOPTED_LOGGERS = {
    "lqabr.system": "system_log",
    "lqabr.process": "process_log",
    "lqabr.audit": "audit_log",
    "lqabr.model": "model_log",
}
_CORE_STREAM = {"audit": "audit_log", "process": "process_log", "system": "system_log"}


class _AdoptRecord(logging.Filter):
    """Normalise an lqabr_core record into our shape.

    Its audit/process/system loggers already carry their fields on
    ``record.lqabr`` (the same attribute we use) and name their stream on
    ``record.log_stream``, so for those this only maps the stream and stamps
    what they do not set. ``lqabr.model`` sets neither — it logs a plain
    printf-style message — so the stream the logger was adopted FOR is the
    fallback, and the message becomes the event."""

    def __init__(self, stream: str) -> None:
        super().__init__()
        self.stream = stream

    def filter(self, record: logging.LogRecord) -> bool:
        payload = dict(getattr(record, "lqabr", None) or {})
        stream = _CORE_STREAM.get(getattr(record, "log_stream", ""), self.stream)
        payload.setdefault("stream", stream)
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        payload.setdefault("agent", AGENT_NAME)
        payload.setdefault("source", "lqabr_core")
        payload.setdefault("level", record.levelname)
        if "event" not in payload:
            url = str(payload.get("url") or payload.get("endpoint") or "")
            # The path alone — the host is the same on every line and the full
            # URL would push the fields off the head.
            if "://" in url:
                url = "/" + url.split("://", 1)[1].split("/", 1)[-1]
            payload["event"] = url or record.getMessage()
        record.lqabr = payload
        record.lqabr_stream = stream
        record.lqabr_glyph = HOP if stream == "audit_log" else SUB
        return True


def _adopt_foreign_loggers() -> None:
    """Route lqabr_core's four loggers through our handlers.

    lqabr_core's ``configure()`` is guarded by a module global, so calling it
    first marks it configured and it will never re-install the handler we
    remove."""
    try:
        from lqabr_core import observability as core  # noqa: WPS433 — optional
        core.configure()
    except Exception:  # noqa: BLE001 — logging must never break the run
        pass
    for name, stream in _ADOPTED_LOGGERS.items():
        logger = logging.getLogger(name)
        logger.handlers.clear()
        if not any(isinstance(f, _AdoptRecord) for f in logger.filters):
            logger.addFilter(_AdoptRecord(stream))
        logger.setLevel(_LOG.level)
        logger.propagate = False
        for handler in _LOG.handlers:
            logger.addHandler(handler)


class _StreamFilter(logging.Filter):
    """Send one stream to its own file. A record carrying no stream — a stray
    from a child logger such as ``lqabr.email.service`` — lands in system_log,
    which is where JsonFormatter files it as well."""

    def __init__(self, stream: str) -> None:
        super().__init__()
        self.stream = stream

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "lqabr_stream", "system_log") == self.stream


class ConsoleFormatter(logging.Formatter):
    """The aligned view: time, glyph, event, outcome, then key=value pairs."""

    WIDTH = 96      # normal mode: wrap the key=value run at this column
    #: Field lines hang under the EVENT column (8 stamp + 1 + 5 glyph + 1),
    #: never under the timestamp — the left edge stays a clean column of times.
    INDENT = 15

    def __init__(self, colour: bool = False) -> None:
        super().__init__()
        self.colour = colour

    def _paint(self, text: str, code: Optional[str]) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour and code else text

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "lqabr", None)
        if payload is None:
            return record.getMessage()
        glyph = getattr(record, "lqabr_glyph", None) or SUB
        stamp = str(payload.get("ts", ""))[11:19] or "--:--:--"
        event = str(payload.get("event") or payload.get("stream", ""))

        outcome = str(payload.get("outcome") or "")
        # A boundary hop reads as one line: METHOD status ms. Its endpoint is
        # already the event name, so repeating it below would say it twice.
        hop = payload.get("stream") == "audit_log"
        if hop:
            outcome = " ".join(str(payload.get(k) or "") for k in
                               ("method", "status_code")).strip()
        elapsed = payload.get("duration_ms")
        if elapsed is not None:
            outcome = f"{outcome} {round(float(elapsed))}ms".strip()

        # Fixed widths, or IN (4 chars) and OUT (5) knock the columns askew.
        # The padding is kept — normal mode puts pairs right after it.
        # The lead's id belongs in the head: it is the subject of the line,
        # not one more field to scan past on every single event.
        subject = str(payload.get("objectId") or "")
        head = (f"{stamp} {self._paint(f'{glyph:<5}', _COLOUR.get(glyph))} "
                f"{event:<20} {subject:<14}")
        if outcome:
            head = f"{head} {outcome}"

        verbose = debug_mode()
        quiet = set(_CONSOLE_QUIET) if event != "run_started" else {"step"}
        if hop:
            quiet |= {"direction", "endpoint", "method", "status_code"}
        items = [(k, v) for k, v in payload.items()
                 if k not in _HEAD_KEYS and k not in quiet
                 and (verbose or (v is not None and v != ""))]
        if not items:
            return head.rstrip()

        # DEBUG: one leaf per line, key column aligned, indented under the
        # event. Reading a 20-field profile as a wrapped blob is how detail
        # turns into noise.
        if verbose:
            leaves = [leaf for key, value in items for leaf in _expand(key, value)]
            # Short events stay on one line — do not explode `state: working`.
            if (len(leaves) <= _INLINE_MAX_FIELDS
                    and sum(len(p) + len(t) for p, t in leaves) <= _INLINE_MAX_CHARS):
                inline = "  ".join(f"{self._paint(f'{p}:', '90')} {t}" for p, t in leaves)
                return f"{head} {inline}".rstrip()
            width = min(max(len(p) for p, _ in leaves) + 1, 26)
            gutter = self.INDENT + width + 1
            room = max(40, _terminal_width() - gutter)
            lines = [head.rstrip()]
            for path, text in leaves:
                label = self._paint(f"{path + chr(58):<{width}}", "90")
                # Hanging indent: the value keeps its own column however long
                # it runs, so the timestamps stay a clean left-hand column.
                # break_on_hyphens=False: "human-oversight" must not come back
                # as "human- oversight" when someone copies a prompt out.
                rows = textwrap.wrap(text, width=room,
                                     break_on_hyphens=False) or [""]
                lines.append(f"{'':>{self.INDENT}}{label} {rows[0]}".rstrip())
                lines.extend(f"{'':>{gutter}}{row}" for row in rows[1:])
            return "\n".join(lines)

        # NORMAL: compact key=value, wrapped — one event stays one glance.
        pairs = [f"{k}={_flat(v)}" for k, v in items]
        chunks, current = [], ""
        for pair in pairs:
            if current and len(current) + len(pair) + 1 > self.WIDTH:
                chunks.append(current)
                current = pair
            else:
                current = f"{current} {pair}".strip()
        if current:
            chunks.append(current)
        # First chunk rides the head line; the rest hang under the event column.
        return "\n".join([f"{head} {chunks[0]}"] + [f"{'':>15}{c}" for c in chunks[1:]])


@dataclass(frozen=True)
class RunContext:
    """The correlation token. run_id rides on the Mailgun message, so the
    event returning days later logs under the same pair."""

    objectId: str
    run_id: str


def _log_directory() -> str:
    """Where the JSON file goes: ``logs/agents/email``, alongside the research
    and summary agents. Empty means stdout only — which is what Cloud Run
    wants, since its filesystem is ephemeral and Cloud Logging reads stdout."""
    configured = os.environ.get("LQABR_EMAIL_LOG_DIR")
    if configured is not None:
        return configured.strip()
    if os.environ.get("K_REVISION"):
        return ""
    return str(_REPO_ROOT / "logs" / "agents" / "email")


def configure_logging(mode: Optional[str] = None, log_dir: Optional[str] = None) -> None:
    """Install the handlers. THE ONE OWNER — callers must not touch _LOG.

    stdout gets the aligned console view (JSON when not a terminal, so Cloud
    Logging can parse it); the file always gets JSON lines. Idempotent."""
    resolved = (mode or log_mode()).strip().lower()
    # Two levels, and only two: normal -> INFO, debug -> DEBUG.
    _LOG.setLevel(logging.DEBUG if resolved == "debug" else logging.INFO)
    _LOG.propagate = False
    if _LOG.handlers:
        _adopt_foreign_loggers()   # cheap and idempotent; keeps them in step
        return

    wanted = os.environ.get("LQABR_EMAIL_LOG_FORMAT", "auto").strip().lower() or "auto"
    if wanted not in LOG_FORMATS:
        raise RuntimeError(
            f"LQABR_EMAIL_LOG_FORMAT={wanted!r} is not one of {'|'.join(LOG_FORMATS)}")
    tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    as_console = wanted == "console" or (wanted == "auto" and tty)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(ConsoleFormatter(colour=tty) if as_console else JsonFormatter())
    _LOG.addHandler(stream_handler)

    directory = _log_directory() if log_dir is None else log_dir.strip()
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        for stream, filename in LOG_FILES.items():
            handler = RotatingFileHandler(
                target / filename, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
            handler.setFormatter(JsonFormatter())  # never the console view — no ANSI in a file
            handler.addFilter(_StreamFilter(stream))
            _LOG.addHandler(handler)

    # Last, so the adopted loggers inherit every handler installed above.
    _adopt_foreign_loggers()


def _emit(stream: str, ctx: Optional[RunContext], *, glyph: str = SUB,
          level: int = logging.INFO, **fields: Any) -> None:
    """Build the event once. Formatters decide how it looks; nothing here is
    concatenated into a message string."""
    payload: Dict[str, Any] = {
        "stream": stream, "ts": datetime.now(timezone.utc).isoformat(),
        "agent": AGENT_NAME,
        "objectId": ctx.objectId if ctx else None,
        "run_id": ctx.run_id if ctx else None,
        **fields,
    }
    if not debug_mode():
        payload = {k: v for k, v in payload.items() if v is not None}
    _LOG.log(level, "", extra={"lqabr": payload, "lqabr_stream": stream,
                               "lqabr_glyph": glyph})


def log_system(**fields: Any) -> None:
    fields.setdefault("host", os.environ.get("K_REVISION") or os.environ.get("HOSTNAME", "local"))
    _emit("system_log", None, **fields)


def log_process(ctx: Optional[RunContext], *, event: str, step: Optional[int] = None,
                glyph: str = SUB, debug_only: bool = False, **fields: Any) -> None:
    """``debug_only=True`` marks a sub-event: logged at DEBUG, so it surfaces
    in debug mode alone. NOT named ``detail`` — that is a data field used
    throughout the agent and would be swallowed as a parameter."""
    _emit("process_log", ctx, glyph=glyph,
          level=logging.DEBUG if debug_only else logging.INFO,
          step=step, event=event, **fields)


def log_audit(ctx: Optional[RunContext], *, direction: str, endpoint: str,
              step: Optional[int] = None, method: str = "",
              status_code: Optional[int] = None, bearer: Optional[str] = None,
              **fields: Any) -> None:
    """A boundary hop. The bearer is fingerprinted, never logged. Stays at INFO
    in both modes — the audit stream is the trail of who was called with which
    credential, and dropping it in normal mode would gut the log file."""
    fingerprint = hashlib.sha256(bearer.encode()).hexdigest()[:12] if bearer else "none"
    # The endpoint names the line — "audit_log" as a heading says nothing.
    fields.setdefault("event", endpoint)
    _emit("audit_log", ctx, glyph=HOP,
          step=step, direction=direction, endpoint=endpoint,
          method=method, status_code=status_code, bearer_fingerprint=fingerprint, **fields)


def model_content_logging_enabled() -> bool:
    """Prompt and completion carry prospect PII. Debug mode turns them on;
    the legacy flag still forces them on in normal mode."""
    return debug_mode() or os.environ.get(
        "LQABR_EMAIL_LOG_MODEL_CONTENT", "").strip().lower() in ("1", "true", "yes", "on")


def log_model(ctx: Optional[RunContext], *, model_name: str, step: int = 6,
              input_tokens: Optional[int] = None, output_tokens: Optional[int] = None,
              prompt: Optional[str] = None, completion: Optional[str] = None,
              **fields: Any) -> None:
    if model_content_logging_enabled():
        fields.update(prompt=prompt, completion=completion, content_logged=True)
    else:
        fields["content_logged"] = False
    fields.setdefault("event", "model_response")
    _emit("model_log", ctx, step=step, model=model_name,
          input_tokens=input_tokens, output_tokens=output_tokens, **fields)


@contextmanager
def span(ctx: Optional[RunContext], name: str, *, step: Optional[int] = None,
         **fields: Any) -> Iterator[Dict[str, Any]]:
    """Bracket a phase: IN on entry, OUT with elapsed ms on exit — including
    the failure path, so a span ALWAYS closes. Update the yielded dict to put
    results on the OUT line.

        with span(ctx, "load_leads", step=5) as out:
            ...
            out["leads_found"] = len(profiles)
    """
    log_process(ctx, event=name, step=step, glyph=IN, **fields)
    # The subject carries to the closing line too. Without this an OUT falls
    # back to the RUN's objectId, so a per-lead span inside a batch closed
    # under the trigger id instead of the lead it just read.
    subject = {"objectId": fields["objectId"]} if "objectId" in fields else {}
    result: Dict[str, Any] = {}
    started = time.perf_counter()
    try:
        yield result
    except BaseException as exc:
        log_process(ctx, event=name, step=step, glyph=FAIL, outcome="failed",
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    error=type(exc).__name__, **{**subject, **result})
        raise
    log_process(ctx, event=name, step=step, glyph=OUT, outcome="ok",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                **{**subject, **result})


def bind_run(objectId: str, run_id: Optional[str] = None) -> RunContext:
    """Mint the run context. objectId is mandatory — without it an event
    cannot be attributed back to a lead."""
    if not objectId:
        raise ValueError("objectId is required — a run cannot be logged without it")
    ctx = RunContext(objectId=str(objectId), run_id=run_id or uuid.uuid4().hex)
    log_process(ctx, step=3, event="run_started", glyph=START, mode=log_mode())
    return ctx


class MCPObservability:
    """Sink handed to mcp/hubspot — it is shared and cannot import agent
    code, so it logs through this instead."""

    def __init__(self, ctx: Optional[RunContext] = None) -> None:
        self.ctx = ctx

    def process(self, **fields: Any) -> None:
        log_process(self.ctx, **fields)

    def audit(self, **fields: Any) -> None:
        log_audit(self.ctx, **fields)


__all__ = [
    "AGENT_NAME", "RunContext", "MCPObservability",
    "configure_logging", "bind_run",
    "log_system", "log_process", "log_audit", "log_model",
    "model_content_logging_enabled", "log_mode", "debug_mode",
    "span", "fields", "preview",
    "ConsoleFormatter", "JsonFormatter",
    "LOG_MODES", "LOG_FORMATS", "LOG_FILES", "LOG_FILE_NAME",
    "IN", "OUT", "FAIL", "SUB", "HOP", "OK", "BUSY", "START",
]
