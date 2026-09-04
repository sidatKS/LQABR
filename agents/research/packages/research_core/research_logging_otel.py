"""ResearchLoggingOtel — files, console, and OTLP, all three, one call: structured, correlated, secret-free.

**Everything above the `the OpenTelemetry OTLP sink` banner is byte-for-byte
`research_logging.py`** — the redaction rules, the three streams, the step
frame, the hop, the console renderer — so a fix ported into one reads
straight across into the other. Below the banner is this module's own thing:
OTLP export was added ON TOP of a working file sink, not instead of it, so
`configure_logging()` here builds files (one fixed name per stream — see
`stream_path`, no day-split naming), the console, AND the OTLP exporter,
every call. `configure_logging()` keeps its exact signature so this drops in
as

    from research_core import research_logging_otel as research_logging

**agents/summary/packages/summary_core/summary_logging.py is a deliberate copy of
the file-sink version of this module.** Same structure, its own names: module `summary_logging.py`, class
`SummaryLogging`, logger `lqabr.summary`, `sum-` run ids, `LQABR_SUMMARY_` env.
When porting a fix, substitute those four and apply it there too.

Do not extract the two into `packages/lqabr_core`. `tests/test_standalone.py` in
both agents exists precisely to assert that neither imports from the shared
package, and that standalone-ness is a decision this project has already made
and paid for. The duplication costs applying a future logging fix twice; it buys
neither agent being able to break the other. If you are here to "clean this up",
that is the reason not to. Keep the two in step by PORTING, not by importing.

---

Three streams, one run_id correlating them (matching the Summary Agent):

    process   what the agent did and why (steps, decisions, counts)
    audit     every hop that left this process (MCP call, model call), with
              endpoint, method, status, duration and the PARAMETERS sent —
              never a credential
    system    startup/shutdown and coarse resource facts

OTLP export is an ADD-ON to this module's existing behaviour, not a
replacement for it: console, the fixed per-stream files, and the OTLP export
all run at once from one `configure_logging()` call. Nothing that worked
before this module gained an exporter stops working now. The files are
plain and uncapped (one fixed name per stream, no rotation), told
apart by three filenames, the same way they always were; the exported records
are additionally told apart by a `log_group` attribute (`process` | `audit` |
`system`), because a collector backend has no filesystem to put three files
in. Every redacted field rides along as an OTel attribute under `lqabr.`, with
`run_id` unprefixed because it is the correlation key a person actually types
into a search box. `redact()` still runs over every field bag on the way out —
recursively, so a credential nested inside a call's arguments is blanked too,
in the console line, in the file, and in the exported attributes alike.

The exporter is attached to the `lqabr.research` logger, **not** to the Python
root logger: this logger sets `propagate = False`, so the usual OpenTelemetry
recipe of adding a handler to the root would export exactly nothing while
looking perfectly healthy.

Every OpenTelemetry import in this module is at the top of this file, in one
guarded block — the SDK is an optional dependency, so a missing piece becomes
`None` and a named reason rather than an ImportError at agent startup. An
export that cannot be built — no `opentelemetry` installed, no collector
listening — is reported once and the run continues console-only. A batching
exporter also means an exiting process must flush: `shutdown_logging()` is
registered with `atexit`, and `flush_logging()` is there for a CLI or a Job
that wants to be sure before it returns.

**Reading a run.** Every step of the pipeline is framed by a pair:

    ▸ IN   <step>   the inputs that step was handed
    ◂ OUT  <step>   what it produced, and how long it took

and every outbound call carries the parameters it was made with, so "where is
the model called and what did we send it" is answerable from the log alone.
Text payloads (a prompt, a note) appear as a length-marked `*_preview`, never
in full on the console — `preview()` decides how much, and
`LQABR_RESEARCH_LOG_MODE` (terse | normal | debug) decides how much it decides.
"""

from __future__ import annotations

import atexit
import contextvars
import copy
import json
import logging
import os
import sys
import textwrap
import time
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── OpenTelemetry: every import in this module, in one place ────────────────
#
# Guarded, because this module is imported by the agent at startup and the SDK
# is an OPTIONAL dependency (`requirements-otel.txt`, not `requirements.txt`).
# A bare import here would make "no OpenTelemetry installed" mean "no agent",
# and PROJECT_CONTEXT's rule is that observability never kills a run. Missing
# pieces become `None`, `configure_logging()` says so by name, and the agent
# runs console-only.
#
# The handler is mid-move between two packages and the obvious import line is
# the one that does not work. Verified against opentelemetry-sdk 1.44.0 and
# opentelemetry-instrumentation-logging 0.65b0:
#
#   * `from opentelemetry.instrumentation.logging import LoggingHandler` —
#     what the SDK's own deprecation notice tells you to write — raises
#     ImportError. That package's `__init__` exports `LoggingInstrumentor`,
#     which injects trace ids into a log FORMAT string, and does not
#     re-export the handler.
#   * `opentelemetry.instrumentation.logging.handler.LoggingHandler`, the
#     SUBMODULE, is the real current handler. This is what runs today.
#   * `opentelemetry.sdk._logs.LoggingHandler` still works and warns. It is a
#     different class from the one above.
#
# So: the re-export first (a future release is then picked up with no edit),
# the submodule that ships it today, then the deprecated one. All three take
# the same `(level=, logger_provider=)` constructor, which is what makes the
# fallback chain safe.

OTEL_MISSING = ""                    #: why the SDK is unusable, or "" if it is

try:                                 # tracing only - never sets OTEL_MISSING
    from opentelemetry import trace as _otel_trace
except ImportError:
    _otel_trace = None                               # type: ignore[assignment]

try:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource
except ImportError as _exc:          # noqa: N816 - module-level sentinel
    set_logger_provider = LoggerProvider = None      # type: ignore[assignment]
    BatchLogRecordProcessor = Resource = None        # type: ignore[assignment]
    OTEL_MISSING = f"opentelemetry-sdk: {_exc}"

try:
    from opentelemetry.instrumentation.logging import (  # type: ignore
        LoggingHandler)                                  # a future re-export
except ImportError:
    try:
        from opentelemetry.instrumentation.logging.handler import (
            LoggingHandler)                              # where it lives today
    except ImportError:
        try:
            with warnings.catch_warnings():              # the deprecated one
                warnings.simplefilter("ignore", DeprecationWarning)
                from opentelemetry.sdk._logs import LoggingHandler
        except ImportError as _exc:
            LoggingHandler = None                        # type: ignore[assignment]
            OTEL_MISSING = OTEL_MISSING or f"no LoggingHandler: {_exc}"

# One exporter per protocol, and they are separate installable packages — so
# they are imported separately and the missing one is only an error if it is
# the one `LQABR_RESEARCH_OTLP_PROTOCOL` actually asks for.
try:
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
        OTLPLogExporter as OTLPGrpcLogExporter)
except ImportError:
    OTLPGrpcLogExporter = None                           # type: ignore[assignment]

try:
    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter as OTLPHttpLogExporter)
except ImportError:
    OTLPHttpLogExporter = None                           # type: ignore[assignment]

#: The exporter for a protocol, or None when that package is not installed.
EXPORTERS = {"grpc": OTLPGrpcLogExporter, "http": OTLPHttpLogExporter}


#: Names that hold a credential VALUE.
_SECRET_VALUE_HINTS = ("token", "secret", "password", "api_key", "apikey",
                       "authorization", "auth", "bearer", "credential")

#: ...but these NAME a credential rather than holding one, and the rule is
#: "log the credential's name, never its value". Blanking the name is the exact
#: inverse: it hid `secret_resolved`'s only field — the line whose whole job is
#: to say WHICH credential came from where — and `secrets_source` in the boot
#: config, which is the first thing you want when a key does not resolve.
_IDENTIFIER_NAMES = ("secret", "credential", "secrets_source")
#: `_name` is here because a field called `secret_name` holds exactly that — a
#: name — and blanking it is the inverse of the rule. Summary's `agent_build`
#: emits two such fields on every boot and they printed as `<redacted>`.
_IDENTIFIER_SUFFIXES = ("_secret", "_secret_name", "_name", "_source", "_ref")

#: A token COUNT is a number the log exists to show, not a token.
_SAFE_NAMES = ("tokens", "max_tokens", "input_tokens", "output_tokens",
               "total_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens", "token_count")


def _holds_a_secret(lowered: str) -> bool:
    """True only for a field that carries the credential ITSELF."""
    if lowered in _SAFE_NAMES or lowered in _IDENTIFIER_NAMES:
        return False
    if lowered.endswith(_IDENTIFIER_SUFFIXES):
        return False
    return any(hint in lowered for hint in _SECRET_VALUE_HINTS)

_MAX_VALUE_CHARS = 500

#: How much of a text payload a `*_preview` field carries.
_PREVIEW_CHARS = 240

#: One ordered axis, replacing the old detail boolean.
#:
#:   terse    no previews, no parameter bags — counts and names only
#:   normal   240-char previews, summarised bags — today's shape (default)
#:   debug    the values themselves, whole and unmangled
#:
#: Debug is not "more logging". Every truncation below happens BEFORE
#: json.dumps, so a structured file has been storing damaged strings in
#: well-formed fields; debug stops the values arriving pre-mangled.
MODES = ("terse", "normal", "debug")
_MODE = "normal"


def set_mode(mode: str) -> None:
    """Set the detail axis process-wide. An unknown value falls back to normal
    rather than raising — a logging setting must not stop a run."""
    global _MODE
    _MODE = mode if mode in MODES else "normal"


def current_mode() -> str:
    return _MODE


def debugging() -> bool:
    return _MODE == "debug"


def set_detail(enabled: bool) -> None:
    """Deprecated alias for the old boolean: 0 -> terse, 1 -> normal."""
    set_mode("normal" if enabled else "terse")


def new_run_id() -> str:
    return f"res-{uuid.uuid4().hex[:12]}"


def preview(text: Any, limit: int = _PREVIEW_CHARS) -> str:
    """A sample of a payload — or, in debug, the payload.

    terse   ""   the field does not print at all
    normal  a single line, whitespace collapsed, trimmed with an ASCII marker
            (ASCII on purpose: this string reaches a cp1252 Windows console)
    debug   the value verbatim — no trim, no marker, and crucially no
            whitespace collapse, which was silently rewriting the payload
    """
    if _MODE == "terse":
        return ""
    body = str(text or "")
    if _MODE == "debug":
        return body
    body = " ".join(body.split())
    if not body:
        return ""
    if len(body) <= limit:
        return body
    return f"{body[:limit]}... (+{len(body) - limit} chars)"


def summarize_args(arguments: Any, head: int = 60) -> Dict[str, Any]:
    """Call arguments, safe to log: long text becomes `[N chars] <head>…`.

    The point is to answer "what did we send?" without pasting a 4,000-character
    note into every line. Names still go through `redact()` on the way out.
    """
    if not isinstance(arguments, dict):
        return {}
    if _MODE == "terse":
        return {"keys": sorted(str(k) for k in arguments)}
    if _MODE == "debug":
        return dict(arguments)          # the values themselves
    out: Dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > head:
            out[str(key)] = f"[{len(value)} chars] {' '.join(value.split())[:head]}..."
        else:
            out[str(key)] = value
    return out


def redact(fields: Dict[str, Any], _depth: int = 0) -> Dict[str, Any]:
    """Blank anything whose NAME suggests a credential; trim long values.

    Name-based, not value-based, on purpose: a token cannot be recognised by
    looking at it, but we always know what we called it. Recursive, because a
    parameter bag is a dict inside a field — a token nested one level down must
    not escape the rule that blanks it at the top.
    """
    clean: Dict[str, Any] = {}
    for key, value in fields.items():
        lowered = str(key).lower()
        if _holds_a_secret(lowered):
            clean[key] = "<redacted>" if value else ""
            continue
        if isinstance(value, dict) and _depth < 4:
            clean[key] = redact(value, _depth + 1)
        elif isinstance(value, (list, tuple)) and _depth < 4:
            clean[key] = [redact(item, _depth + 1) if isinstance(item, dict) else item
                          for item in value]
        elif (isinstance(value, str) and _MODE != "debug"
                and len(value) > _MAX_VALUE_CHARS):
            clean[key] = value[:_MAX_VALUE_CHARS] + f"... (+{len(value) - _MAX_VALUE_CHARS} chars)"
        else:
            clean[key] = value
    return clean


class _Stream:
    def __init__(self, name: str, run_id: str, logger: logging.Logger) -> None:
        self._name = name
        self._run_id = run_id
        self._logger = logger

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "stream": self._name,
            "run_id": self._run_id,
            "event": event,
            "ts": time.time(),
            **redact(fields),
        }
        # The JSON string IS the message, so the file handler needs no work.
        # The record rides along on `extra` so the console formatter can render
        # it without parsing its own output back.
        self._logger.info(json.dumps(record, default=repr, ensure_ascii=False),
                          extra={"lqabr_record": record})


class Step:
    """What a step produced. Set it before returning; the frame reports it."""

    __slots__ = ("status", "fields")

    def __init__(self) -> None:
        self.status = "ok"
        self.fields: Dict[str, Any] = {}

    def ok(self, **fields: Any) -> None:
        self.status, self.fields = "ok", fields

    def failed(self, reason: str, **fields: Any) -> None:
        self.status, self.fields = "failed", {"reason": reason, **fields}

    def skipped(self, reason: str, **fields: Any) -> None:
        self.status, self.fields = "skipped", {"reason": reason, **fields}




@dataclass
class ResearchLoggingOtel:
    """One run's logging handle. Build one per run; pass it down."""

    run_id: str = field(default_factory=new_run_id)
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("lqabr.research"))

    def __post_init__(self) -> None:
        # One child logger per stream, so each can carry its OWN file handler
        # while the console keeps a single interleaved narrative: children
        # propagate up to whatever is attached to the parent.
        self.process = _Stream("process", self.run_id, self.logger.getChild("process"))
        self.audit = _Stream("audit", self.run_id, self.logger.getChild("audit"))
        self.system = _Stream("system", self.run_id, self.logger.getChild("system"))

    # ---------------------------------------------------------------- steps
    @contextmanager
    def step(self, name: str, **inputs: Any):
        """Frame one step: its inputs, its outputs, its duration.

            with obs.step("read_lead", objectId=oid, tool=tool) as step:
                lead = hubspot.read_lead(oid)
                if lead is None:
                    step.failed(reason)
                    return ...
                step.ok(company=lead.company)

        The frame closes itself — on the way out of the block, on an early
        return, and on an exception (which is recorded as the failure it is
        before it propagates). A step that opens can therefore never be left
        open, which is the one bug a caller-threaded start/stop pair invites.
        """
        self.process.emit("step_in", step=name, **inputs)
        outcome = Step()
        started = time.monotonic()
        try:
            yield outcome
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            outcome.failed(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.process.emit("step_out", step=name, status=outcome.status,
                              duration_ms=round((time.monotonic() - started) * 1000, 1),
                              **outcome.fields)

    # ---------------------------------------------------------------- hops
    def hop(self, *, service: str, endpoint: str, method: str = "POST",
            status: Optional[int] = None, duration_ms: Optional[float] = None,
            attempt: int = 1, error: str = "",
            params: Optional[Dict[str, Any]] = None,
            usage: Optional[Dict[str, Any]] = None) -> None:
        """One outbound call, on the audit stream.

        `params` is what we SENT — the tool name and its arguments, the model
        and its knobs — summarised, redacted, never the raw payload and never a
        credential. Without it the log says a call happened but not what it
        asked for, which is the question a person actually has.

        `usage` is what the call COST, for a call to a metered service: token
        counts and billable server-tool requests, as top-level fields. The
        dividing line that keeps this from becoming a dumping ground is one
        sentence: **audit records what the call cost; process records what the
        call produced.** So counts belong here and `stop_reason`, `searches`
        and `chars` do not. A call that RAISED produced no usage, and its hop
        carries none — that is correct, not a gap.
        """
        counted = {name: value for name, value in (usage or {}).items()
                   if value is not None}
        self.audit.emit("outbound_call", service=service, endpoint=endpoint,
                        method=method, status=status, duration_ms=duration_ms,
                        attempt=attempt, error=error,
                        params=(params or {}) if _MODE != "terse" else {},
                        **counted)


#: Per-context, NOT per-process. Two campaigns in flight — a webhook and a
#: redelivery — used to fight over one module global, and whichever started
#: last owned it: every lazy `get_obs()` after that, including the route
#: handlers' own audit lines, was stamped with an unrelated run's id.
_OBS: contextvars.ContextVar["ResearchLoggingOtel | None"] = contextvars.ContextVar(
    "lqabr_research_obs", default=None)


# ── console rendering ───────────────────────────────────────────────────────
# Only the CONSOLE is reshaped. The log file stays JSON lines, always.

_DIM, _RESET = "\033[2m", "\033[0m"
_STREAM_COLOUR = {"process": "\033[36m", "audit": "\033[2m", "system": "\033[35m"}
_RED, _YELLOW, _GREEN = "\033[31m", "\033[33m", "\033[32m"
_BLUE = "\033[34m"

_SKIP = ("stream", "run_id", "event", "ts")
#: Rendered by the step branch itself, ahead of the ordinary fields.
_STEP_SKIP = _SKIP + ("step", "status", "duration_ms")
#: Nothing may wrap. A wrapped line redraws over its neighbour and the log
#: becomes unreadable exactly when a run is busiest. Measured from the real
#: terminal, with a sane fallback when there isn't one.
_MAX_LINE = 165

#: Blank lines printed after a finished lead. Two, because one reads as an
#: accident and two reads as a deliberate break.
_LEAD_GAP = "\n\n"

_BAD = ("failed", "stopped", "error", "unreachable", "rejected")
_MEH = ("skipped", "degraded", "dropped", "override")


def _terminal_width(fallback: int = _MAX_LINE) -> int:
    try:
        import shutil
        return max(90, shutil.get_terminal_size(fallback=(fallback, 24)).columns - 1)
    except Exception:  # noqa: BLE001 - width is a nicety, never a failure
        return fallback

# Windows consoles default to cp1252, which cannot encode these — a log line
# must never be the thing that raises. Chosen per stream, not assumed.
_GLYPHS_UNICODE = {"call": "→", "ok": "✓", "bad": "✗", "warn": "!", "plain": "·",
                   "in": "▸", "out": "◂", "cut": "…"}
_GLYPHS_ASCII = {"call": "->", "ok": "+", "bad": "x", "warn": "!", "plain": ".",
                 "in": ">", "out": "<", "cut": "..."}


def _glyphs_for(stream: Any) -> Dict[str, str]:
    encoding = getattr(stream, "encoding", "") or ""
    try:
        "".join(_GLYPHS_UNICODE.values()).encode(encoding)
    except (LookupError, UnicodeEncodeError, TypeError):
        return _GLYPHS_ASCII
    return _GLYPHS_UNICODE


#: Fields that carry the diagnosis, or the payload a person came to read.
#: Capping these at the ordinary field width is exactly backwards — they matter
#: most when they are longest. They render LAST and take whatever room is left,
#: continuing on indented lines rather than being cut.
_DIAGNOSTIC = ("reason", "error", "detail")
_SPILL_SUFFIXES = ("_preview",)

_FIELD_CHARS = 90


def _spills(key: str) -> bool:
    """True for a field that must never be truncated away."""
    return key in _DIAGNOSTIC or str(key).endswith(_SPILL_SUFFIXES)


def _value(value: Any, cap: int = _FIELD_CHARS, cut: str = "...") -> str:
    """One field, short enough to sit on a terminal line.

    In debug nothing is cut here: every field is routed to the continuation
    lines instead, so the value survives whole and the width invariant still
    holds. See `_fields`.
    """
    if _MODE == "debug":
        return str(value)
    if isinstance(value, dict):
        inner = ", ".join(f"{k}={_value(v, 48, cut)}" for k, v in list(value.items())[:6])
        extra = len(value) - 6
        return "{" + inner + (f", +{extra}" if extra > 0 else "") + "}"
    if isinstance(value, (list, tuple)):
        head = ", ".join(str(v) for v in list(value)[:3])
        extra = len(value) - 3
        return f"[{head}{f' +{extra}' if extra > 0 else ''}]"
    text = str(value).replace("\n", " ")
    return text if len(text) <= cap else text[:max(1, cap - len(cut))] + cut


class ConsoleFormatter(logging.Formatter):
    """One event, one readable line — the shape a person scanning a run needs.

    Falls back to the raw message for anything not emitted through a _Stream
    (a stray library warning), so nothing is ever swallowed.
    """

    def __init__(self, colour: bool = True,
                 glyphs: Optional[Dict[str, str]] = None,
                 width: int = _MAX_LINE) -> None:
        super().__init__("%(message)s")
        self._colour = colour
        self._g = dict(glyphs or _GLYPHS_ASCII)
        self._cut = self._g.get("cut", "...")
        self._width = max(90, int(width))

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self._colour else text

    # -- field rendering, shared by every branch ---------------------------
    def _fields(self, data: Dict[str, Any], plain_head: str,
                skip: Tuple[str, ...],
                used: int = 0) -> Tuple[List[str], List[Tuple[str, str]]]:
        """key=value pairs trimmed against the PLAIN width (colour codes are
        invisible on screen but would otherwise eat most of the budget).

        Returns (rendered, spill); spill is what continues on its own lines.
        """
        pairs = [(k, v) for k, v in data.items()
                 if k not in skip and v not in ("", None, [], {})]

        if _MODE == "debug":
            # Nothing is trimmed and nothing is dropped: every field joins the
            # class that already continues on indented lines, and a nested dict
            # or list becomes one key per line rather than `{a=1, b=2, +3}`.
            spilled: List[Tuple[str, str]] = []
            for key, value in pairs:
                if isinstance(value, dict):
                    for inner, held in value.items():
                        spilled.append((f"{key}.{inner}", str(held)))
                elif isinstance(value, (list, tuple)):
                    for index, held in enumerate(value):
                        spilled.append((f"{key}[{index}]", str(held)))
                else:
                    spilled.append((key, str(value)))
            return [], spilled

        budget = max(20, self._width - len(plain_head))
        # A diagnosis or a payload preview reads LAST and keeps whatever room is
        # left, so it is never cut in favour of a short bookkeeping field.
        pairs.sort(key=lambda kv: _spills(kv[0]))
        rendered: List[str] = []
        spill: List[Tuple[str, str]] = []
        full = False
        for key, value in pairs:
            if _spills(key):
                text = str(value).replace("\n", " ").strip()
                room = budget - used - len(key) - 2
                if not full and len(text) <= room:
                    rendered.append(f"{self._paint(key, _DIM)}={text}")
                    used += len(key) + 1 + len(text) + 1
                else:
                    # A deliberate wrap onto indented lines — not the accidental
                    # kind that redraws over its neighbour.
                    spill.append((key, text))
                continue
            if full:
                # The line is out of room. Ordinary fields stop here, but the
                # loop does NOT: a diagnosis or a payload preview sorts last and
                # must still reach the continuation lines below.
                continue
            piece = f"{key}={_value(value, _FIELD_CHARS, self._cut)}"
            if used + len(piece) + 1 > budget:
                # The marker itself costs width. Print it only if it fits —
                # otherwise it is the one character that wraps the line.
                if used + len(self._cut) + 1 <= budget:
                    rendered.append(self._cut)
                    used += len(self._cut) + 1
                full = True
                continue
            rendered.append(f"{self._paint(key, _DIM)}="
                            f"{_value(value, _FIELD_CHARS, self._cut)}")
            used += len(piece) + 1
        return rendered, spill

    def _continue(self, line: str, spill: List[Tuple[str, str]], colour: str) -> str:
        for key, text in spill:
            indent = " " * 11
            # `break_on_hyphens=False`: the default splits `UNIQUE-TAIL-MARKER`
            # across two lines, so a value copied back out of the console comes
            # back mangled — which is the exact thing debug mode exists to stop.
            # `break_long_words` stays TRUE: a single token wider than the
            # terminal must still be broken, because the width invariant is a
            # hard rule and hyphen-prettiness is not.
            for chunk in textwrap.wrap(f"{key}: {text}", width=self._width - 11,
                                       subsequent_indent="  ",
                                       break_on_hyphens=False) or [f"{key}:"]:
                line += "\n" + indent + self._paint(chunk, colour)
        return line

    # -- one record --------------------------------------------------------
    def format(self, record: logging.LogRecord) -> str:
        data = getattr(record, "lqabr_record", None)
        if not isinstance(data, dict):
            return record.getMessage()

        clock = time.strftime("%H:%M:%S", time.localtime(data.get("ts", time.time())))
        event = str(data.get("event", ""))
        stream = str(data.get("stream", ""))

        # An outbound call has a fixed shape; spell it as one, not as key=value.
        if event in ("outbound_call", "http_out"):
            status = data.get("status", data.get("status_code"))
            took = data.get("duration_ms")
            where = data.get("endpoint") or data.get("url") or ""
            line = (f"{data.get('method', '')} {where} "
                    f"{status if status is not None else '-'}"
                    f"{f' {took:.0f}ms' if isinstance(took, (int, float)) else ''}")
            attempt = data.get("attempt", 1)
            if isinstance(attempt, int) and attempt > 1:
                line += f" (attempt {attempt})"
            # What the call COST. The counts were already riding this record in
            # JSON; without this they were invisible on the console, which is
            # where the question "what did that call cost" actually gets asked.
            # ASCII on purpose — this line reaches a cp1252 Windows console.
            meter = "/".join(str(data[k]) for k in ("input_tokens", "output_tokens")
                             if isinstance(data.get(k), int))
            if meter:
                line += f" {meter} tok"
            if isinstance(data.get("web_search_requests"), int):
                line += f" {data['web_search_requests']}srch"
            head = f"{self._g['call']} {data.get('service', '?'):<9} {line}"
            body = self._paint(head, _DIM)
            # Measured as we go: an error appended blind is how this line used
            # to run off the edge and redraw over its neighbour.
            plain_len = len(clock) + 1 + len(head)
            sent = data.get("params")

            if _MODE == "debug":
                # Same treatment the process branch already gets: nothing is
                # trimmed, but nothing runs off the edge either. Without this a
                # write hop printed the whole 1,600-character note as ONE
                # console line — complete, and unreadable.
                spill: List[Tuple[str, str]] = []
                if isinstance(sent, dict):
                    for key, value in sent.items():
                        if isinstance(value, dict):
                            for inner, held in value.items():
                                spill.append((f"{key}.{inner}", str(held)))
                        elif isinstance(value, (list, tuple)):
                            for index, held in enumerate(value):
                                spill.append((f"{key}[{index}]", str(held)))
                        else:
                            spill.append((key, str(value)))
                if data.get("error"):
                    spill.append(("error", str(data["error"])))
                return self._continue(f"{self._paint(clock, _DIM)} {body}",
                                      spill, _BLUE)

            # WHAT we sent — the tool and its arguments, the model and its knobs.
            if isinstance(sent, dict) and sent:
                text = " ".join(f"{k}={_value(v, 48, self._cut)}" for k, v in sent.items())
                room = self._width - plain_len - 1
                if room > 12:
                    text = _value(text, room, self._cut)
                    body += " " + self._paint(text, _BLUE)
                    plain_len += 1 + len(text)
            if data.get("error"):
                room = min(90, self._width - plain_len - 1)
                if room > 12:
                    body += " " + self._paint(
                        _value(str(data["error"]), room, self._cut), _RED)
            return f"{self._paint(clock, _DIM)} {body}"

        # A step frame: what went IN, what came OUT, how long it took.
        if event in ("step_in", "step_out"):
            incoming = event == "step_in"
            status = str(data.get("status", "") or "")
            mark = self._g["in"] if incoming else (
                self._g["bad"] if status == "failed" else
                self._g["warn"] if status == "skipped" else self._g["out"])
            colour = (_BLUE if incoming else
                      _RED if status == "failed" else
                      _YELLOW if status == "skipped" else _GREEN)
            head = f"{'IN ' if incoming else 'OUT'} {str(data.get('step', '')):<20}"
            lead: List[str] = []
            used = 0
            if not incoming:
                took = data.get("duration_ms")
                tail = f"{status}{f' {took:.0f}ms' if isinstance(took, (int, float)) else ''}"
                lead.append(self._paint(tail, colour))
                used = len(tail) + 1
            plain = f"{clock} {mark} {head} "
            rendered, spill = self._fields(data, plain, _STEP_SKIP, used=used)
            line = (f"{self._paint(clock, _DIM)} {self._paint(mark, colour)} "
                    f"{self._paint(head, colour)} "
                    f"{' '.join(lead + rendered)}".rstrip())
            return self._continue(line, spill, colour)

        # A campaign is a queue, so say where you are in it. "3/5 · 2 left" is
        # the one thing a person watching a long run actually wants.
        if event in ("campaign_lead_start", "campaign_lead_done"):
            done = event.endswith("_done")
            at, total = int(data.get("position", 0)), int(data.get("of", 0))
            status = str(data.get("status", ""))
            mark = (self._g["bad"] if status == "failed" else
                    self._g["ok"] if done else self._g["plain"])
            colour = (_RED if status == "failed" else
                      _GREEN if done else _STREAM_COLOUR.get(stream, ""))
            # "left" only on the DONE line: on the start line it would count
            # the lead currently being worked, which reads as one too many.
            tail = (f" {status} chars={data.get('chars', 0)}"
                    f"  ({max(total - at, 0)} left)" if done else "  working…")
            line = f"lead {at}/{total}".ljust(12) + f"{data.get('objectId', '')}{tail}"
            out = (f"{self._paint(clock, _DIM)} {self._paint(mark, colour)} "
                   f"{self._paint(line, colour)}")
            if data.get("error"):
                room = min(80, self._width - len(clock) - len(mark) - len(line) - 3)
                if room > 12:
                    out += " " + self._paint(
                        _value(str(data["error"]), room, self._cut), _RED)
            if done:
                # A seam between one lead's block and the next. Over five leads
                # the console is ~120 lines of one continuous wall; the eye
                # needs somewhere to land. CONSOLE ONLY — the log file is one
                # JSON object per line and a blank line there breaks a parser.
                out += _LEAD_GAP
            return out

        mark, colour = self._g["plain"], _STREAM_COLOUR.get(stream, "")
        if any(word in event for word in _BAD) or data.get("error"):
            mark, colour = self._g["bad"], _RED
        elif any(word in event for word in _MEH):
            mark, colour = self._g["warn"], _YELLOW
        elif event.endswith(("_ok", "_complete", "_found")):
            mark, colour = self._g["ok"], _GREEN

        plain = f"{clock} {mark} {event:<24} "
        rendered, spill = self._fields(data, plain, _SKIP)
        line = (f"{self._paint(clock, _DIM)} {self._paint(mark, colour)} "
                f"{self._paint(f'{event:<24}', colour)} "
                f"{' '.join(rendered)}".rstrip())
        return self._continue(line, spill, colour)


def _console_handler(log_format: str, stream: Any = None) -> logging.Handler:
    """`auto` means: text when a person is watching, JSON when a machine is.

    stdout is what Cloud Logging ingests, so an unattached stdout keeps its
    structured fields — readability never costs production observability.
    """
    stream = stream or sys.stdout
    handler = logging.StreamHandler(stream)
    tty = bool(getattr(stream, "isatty", lambda: False)())
    wants_text = log_format == "text" or (log_format == "auto" and tty)
    handler.setFormatter(
        ConsoleFormatter(colour=tty, glyphs=_glyphs_for(stream),
                         width=_terminal_width()) if wants_text
        else logging.Formatter("%(message)s"))
    return handler


# ── the OpenTelemetry OTLP sink ─────────────────────────────────────────────
#
# Everything above this line — the redaction rules, the three streams, the
# step frame, the hop, the console renderer — is unchanged and is meant to
# STAY byte-comparable with `research_logging.py`, so a fix ported into one
# can be read straight across into the other. Below this line is this
# module's own file handling: one FIXED, undated file per stream
# (`research_process.log`, not `research_process_2026-09-03.log` — see
# `stream_path`), plain and uncapped — no rotation, no size cap.
# `configure_logging()` builds these handlers AND the OTLP handler AND the
# console handler, every call — three independent sinks, not three
# alternatives.

#: The three streams. Still ours, still a code constant. They are no longer
#: file stems — they are the value of the `log_group` attribute on every
#: exported record, which is how a backend tells one stream from another now
#: that they no longer live in three separate files.
STREAMS = ("process", "audit", "system")

#: Default collector address. gRPC OTLP, the sidecar on localhost — the shape
#: the Cloud Run collector deployment uses.
_OTLP_DEFAULT_ENDPOINT = "localhost:4317"
_OTLP_DEFAULT_HTTP_ENDPOINT = "http://localhost:4318"

#: How deep a nested field is flattened into dotted attribute names before the
#: rest is stringified. `params` is a dict inside a field and a person wants
#: `lqabr.params.tool`, not a JSON blob.
_MAX_ATTR_DEPTH = 4

#: What the sink actually is right now — for `/health`. `dir` and `files` are
#: kept, always empty, because `service_app.py` spreads this dict into its
#: health payload and `tests/test_obs_sinks.py` reads both keys by name. An
#: empty `files` is the honest answer here: this module writes no files.
_SINK_STATE: Dict[str, Any] = {"dir": "", "files": {}, "degraded": [],
                               "exporter": "none", "endpoint": "",
                               "protocol": "", "service_name": "",
                               "headers_from": ""}


def sink_state() -> Dict[str, Any]:
    """What the log sinks actually are right now — for `/health`."""
    return {"dir": _SINK_STATE["dir"],
            "files": dict(_SINK_STATE["files"]),
            "degraded": list(_SINK_STATE["degraded"]),
            "exporter": _SINK_STATE["exporter"],
            "endpoint": _SINK_STATE["endpoint"],
            "protocol": _SINK_STATE["protocol"],
            "service_name": _SINK_STATE["service_name"],
            "headers_from": _SINK_STATE["headers_from"]}


def _degraded(reason: str) -> None:
    """Record a sink problem once. `/health` reports it; the run continues."""
    if reason not in _SINK_STATE["degraded"]:
        _SINK_STATE["degraded"].append(reason)


# ── files — the add-on's foundation, ported verbatim from research_logging.py ──
#
# This is NOT a rewrite: OTLP export was added ON TOP of the working file
# sink, not in place of it, so this section is the file-sink module's own
# file-handling code, byte-for-byte. `configure_logging()` below builds these
# handlers AND the OTLP handler AND the console handler, every call — three
# independent sinks, not three alternatives.

def stream_path(log_dir: str, stream: str) -> str:
    """`<log_dir>/research_process.log` — one fixed file per stream, always
    the same name. No date in it: a day boundary must never change which file
    a `tail -f` is watching, and a service that stays up for a week keeps
    writing the one file someone already has open. The file is uncapped —
    no size cap, no rotation, no day-based sweep — it simply grows."""
    return os.path.join(log_dir, f"research_{stream}.log")


def _file_handler(path: str, stream_name: str) -> Optional[logging.Handler]:
    """One stream's file, or None with a named reason on the system stream.

    Plain, uncapped `FileHandler` — no size cap, no rotation. That also
    removes the Windows `doRollover()` [WinError 32] hazard this used to
    guard against (the service and the CLI can hold the same file open):
    there is no rollover left to fail.
    """
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:
        _degraded(f"{stream_name}:open")
        logging.getLogger("lqabr.research").warning(json.dumps(
            {"stream": "system", "event": "log_sink_unavailable",
             "sink": stream_name, "path": path,
             "reason": f"{type(exc).__name__}: {exc}",
             "detail": "the run continues; this stream is console/OTLP-only"}))
        return None
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._lqabr_path = path  # type: ignore[attr-defined]
    return handler



# ── record → OTel attributes ────────────────────────────────────────────────
# A log record here is already a flat-ish dict of redacted fields. OTel
# attributes are primitives or homogeneous sequences of primitives, so a dict
# handed over whole is dropped by the SDK with a warning per record. These two
# functions are the whole translation, and they are pure so they can be tested
# without a collector, without the SDK, and without a network.

def _scalar(value: Any) -> Any:
    """One attribute value the SDK will accept."""
    if isinstance(value, bool) or isinstance(value, (int, float, str)):
        return value
    return str(value)


def _flatten(prefix: str, value: Any, out: Dict[str, Any], depth: int = 0) -> None:
    if value is None or value == "":
        return
    if isinstance(value, dict):
        if not value:
            return
        if depth < _MAX_ATTR_DEPTH:
            for key, held in value.items():
                _flatten(f"{prefix}.{key}", held, out, depth + 1)
        else:
            out[prefix] = json.dumps(value, default=repr, ensure_ascii=False)
        return
    if isinstance(value, (list, tuple)):
        if not value:
            return
        # Homogeneous on purpose: a mixed list is rejected by the SDK, and a
        # list of strings is still readable in every backend.
        out[prefix] = [str(item) for item in value]
        return
    out[prefix] = _scalar(value)


def otel_attributes(record: Dict[str, Any]) -> Dict[str, Any]:
    """The attributes one emitted record becomes.

    `log_group` carries the stream — process | audit | system — because that
    is the name the rest of the platform's OTel work already uses, and it is
    what a backend filters on now that the three files are gone. `stream` is
    kept alongside it so a query written against our own JSON still matches.

    Everything else is namespaced under `lqabr.` so an agent field can never
    collide with a semantic-convention attribute the SDK or the collector
    sets. `run_id` stays unprefixed: it is the correlation key this whole
    module exists for, and a person types it into a search box.
    """
    # `redact()` again, deliberately. The record reached here already redacted
    # by `_Stream.emit`, and redaction is idempotent — but this function is
    # public, pure and testable, and a caller who hands it a raw bag must not
    # be able to put a credential on the wire. Cheap insurance on the one rule
    # this project does not bend.
    record = redact(dict(record))
    out: Dict[str, Any] = {}
    stream = record.get("stream")
    if stream:
        out["log_group"] = str(stream)
        out["stream"] = str(stream)
    for key in ("run_id", "event"):
        if record.get(key):
            out[key] = str(record[key])
    ts = record.get("ts")
    if isinstance(ts, (int, float)):
        out["ts"] = float(ts)
    for key, value in record.items():
        if key in ("stream", "run_id", "event", "ts"):
            continue
        _flatten(f"lqabr.{str(key)}", value, out)
    return out


# ── the handler ─────────────────────────────────────────────────────────────

#: Built once, kept so `shutdown_logging()` can flush it. A BatchLogRecord-
#: Processor holds records in memory until its interval elapses; a CLI run
#: that exits without flushing exports NOTHING, which is the failure mode this
#: reference is here to prevent.
_PROVIDER: Any = None
_PROVIDER_PUBLISHED = False
_ATEXIT_REGISTERED = False


def current_trace_context() -> Dict[str, str]:
    """`{otelTraceID, otelSpanID}` for the span in flight, or `{}`.

    The SDK's LoggingHandler reads these off the record. It normally sets them
    itself from the ambient span, but this module attaches its OWN handler to
    `lqabr.research` (the logger sets `propagate = False`, so the root handler
    auto-instrumentation installs is never reached), and a record built by
    `_Stream.emit` carries no span context of its own. Reading it here is what
    links a log line to the span it happened inside: click a slow MCP span,
    see the `outbound_call` and `mcp_tool_result` records within it.

    Silent by design. Tracing is optional — no SDK, no auto-instrumentation,
    or no span in flight all mean the same thing to a log record: no ids, and
    nothing said about it.
    """
    if _otel_trace is None:
        return {}
    try:
        ctx = _otel_trace.get_current_span().get_span_context()
        if not ctx or not ctx.is_valid:
            return {}
        return {"otelTraceID": format(ctx.trace_id, "032x"),
                "otelSpanID": format(ctx.span_id, "016x")}
    except Exception:                # noqa: BLE001 - a sink cannot kill a run
        return {}


def _make_stream_handler(base: Any = None) -> Any:
    """`LoggingHandler`, taught the three streams.

    The SDK handler turns whatever it finds in `vars(record)` into attributes.
    Our record rides as ONE key — `lqabr_record`, a dict — which the SDK would
    reject wholesale. So the record is copied, that key is swapped for the flat
    attributes it expands into, and the copy is what goes downstream: the
    original is left untouched, so the console handler still renders it
    whatever order the handlers were added in.
    """

    base = LoggingHandler if base is None else base

    class _OtelStreamHandler(base):  # type: ignore[misc, valid-type]

        def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
            data = getattr(record, "lqabr_record", None)
            if isinstance(data, dict):
                record = copy.copy(record)
                record.__dict__ = dict(record.__dict__)
                record.__dict__.pop("lqabr_record", None)
                record.__dict__.update(otel_attributes(data))
                # Correlation. Set on the COPY, so the console handler and the
                # file sinks keep rendering the record exactly as before.
                record.__dict__.update(current_trace_context())
            try:
                super().emit(record)
            except Exception as exc:  # noqa: BLE001 - a sink cannot kill a run
                _degraded("otlp:emit")
                if _SINK_STATE.get("emit_reported"):
                    return
                _SINK_STATE["emit_reported"] = True
                sys.stderr.write(json.dumps(
                    {"stream": "system", "event": "log_export_failed",
                     "reason": f"{type(exc).__name__}: {exc}",
                     "detail": "the run continues; this is reported once and "
                               "the handler keeps accepting records"}) + "\n")

    return _OtelStreamHandler


def _otlp_handler(*, service_name: str, endpoint: str, protocol: str,
                  insecure: bool, headers: str, headers_from: str,
                  timeout_seconds: int) -> Optional[logging.Handler]:
    """The OTLP handler, or None with a named reason on stderr.

    Every failure here is survivable by design: no OpenTelemetry installed, no
    collector listening, a bad endpoint. The agent falls back to console-only
    and says so, because PROJECT_CONTEXT's rule is that observability never
    kills a run.
    """
    global _PROVIDER, _PROVIDER_PUBLISHED, _ATEXIT_REGISTERED
    exporter_class = EXPORTERS.get(protocol)
    missing = OTEL_MISSING or (
        "" if exporter_class is not None else
        f"opentelemetry-exporter-otlp-proto-{protocol} is not installed")
    if missing:
        # Named, not swallowed: which package, and what to install. The
        # import itself already happened at the top of this file.
        _degraded("otlp:import")
        sys.stderr.write(json.dumps(
            {"stream": "system", "event": "log_sink_unavailable",
             "sink": "otlp", "protocol": protocol, "reason": missing,
             "detail": "the run continues and this agent is console-only. "
                       "pip install -r requirements-otel.txt"}) + "\n")
        return None

    attributes: Dict[str, Any] = {"service.name": service_name,
                                  "lqabr.agent": "research"}

    try:
        resource = Resource.create(attributes)
        # `insecure` is a gRPC channel option and the HTTP exporter has no
        # such parameter — the scheme in the endpoint carries it there.
        options: Dict[str, Any] = {"endpoint": endpoint,
                                   "timeout": max(1, int(timeout_seconds)),
                                   "headers": _headers_dict(headers) or None}
        if protocol == "grpc":
            options["insecure"] = insecure
        exporter = exporter_class(**options)
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    except Exception as exc:  # noqa: BLE001 - a sink cannot kill a run
        _degraded("otlp:build")
        sys.stderr.write(json.dumps(
            {"stream": "system", "event": "log_sink_unavailable",
             "sink": "otlp", "endpoint": endpoint, "protocol": protocol,
             "reason": f"{type(exc).__name__}: {exc}",
             "detail": "the run continues; this agent is console-only"}) + "\n")
        return None

    # Shut the previous one down rather than leaking its exporter thread across
    # a reconfigure — `configure_logging` is idempotent and gets called twice
    # in a test run and once per lifespan in the service.
    _shutdown_provider()
    _PROVIDER = provider
    if not _PROVIDER_PUBLISHED:
        # The global provider, so a library that logs through OTel on its own
        # lands in the same place. Set once: the SDK warns and ignores a
        # second call, and a warning on every reconfigure is noise.
        try:
            set_logger_provider(provider)
            _PROVIDER_PUBLISHED = True
        except Exception:  # noqa: BLE001 - the handler below works regardless
            pass
    if not _ATEXIT_REGISTERED:
        atexit.register(shutdown_logging)
        _ATEXIT_REGISTERED = True

    handler = _make_stream_handler(LoggingHandler)(
        level=logging.NOTSET, logger_provider=provider)
    _SINK_STATE.update({"exporter": "otlp", "endpoint": endpoint,
                        "protocol": protocol, "service_name": service_name,
                        "headers_from": headers_from})
    return handler


def _headers_dict(raw: str) -> Dict[str, str]:
    """`key=value,key2=value2` — the OTLP env-var spelling — as a dict.

    The VALUE never reaches a log line here or anywhere else: only the name of
    the variable it came from is ever recorded (`headers_from` in
    `sink_state()`), which is the project's rule for a credential.
    """
    out: Dict[str, str] = {}
    for pair in str(raw or "").split(","):
        if "=" in pair:
            key, _, value = pair.partition("=")
            key, value = key.strip(), value.strip()
            if key:
                out[key] = value
    return out


def _shutdown_provider() -> None:
    global _PROVIDER
    provider, _PROVIDER = _PROVIDER, None
    if provider is None:
        return
    try:
        provider.shutdown()             # flushes, then stops the export thread
    except Exception:  # noqa: BLE001 - shutting down cannot be the thing that fails
        _degraded("otlp:shutdown")


def flush_logging(timeout_millis: int = 5_000) -> bool:
    """Push everything queued to the collector NOW.

    A `BatchLogRecordProcessor` holds records until its schedule elapses. A CLI
    run, a Cloud Run Job, or a container that exits promptly will otherwise
    drop the entire run's logs — the one failure this sink has that the file
    sink did not. Call it before exiting; `shutdown_logging` calls it for you.
    """
    if _PROVIDER is None:
        return False
    try:
        return bool(_PROVIDER.force_flush(timeout_millis))
    except Exception:  # noqa: BLE001
        _degraded("otlp:flush")
        return False


def shutdown_logging() -> None:
    """Flush and stop. Registered with `atexit` when the sink is built."""
    _shutdown_provider()


# ── configuration ───────────────────────────────────────────────────────────

def _note(event: str, **fields: Any) -> None:
    """One line from the sink itself, on the system stream.

    It matters that these go through `_Stream`, not through
    `root.warning(json.dumps(...))` as the file-sink module does. A bare
    warning carries no `lqabr_record`, so the console renders it as a raw JSON
    blob AND — the part that actually bites — it reaches the exporter with no
    `log_group`, no `run_id` and no attributes at all. The one place the sink
    talks about itself would be the one place a backend query misses.
    """
    _Stream("system", get_obs().run_id,
            logging.getLogger("lqabr.research").getChild("system")).emit(
                event, **fields)


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def configure_logging(level: str = "INFO", log_dir: str = "",
                      log_format: str = "auto", detail: bool = True,
                      max_bytes: int = 52_428_800, backups: int = 5,
                      log_file: str = "", mode: str = "",
                      console: Any = None, retention_days: int = 7) -> None:
    """Readable console on the parent, OTLP export on the same parent.

        lqabr.research                 <- ConsoleFormatter + OTLP, propagate=False
        ├── lqabr.research.process     -> log_group=process
        ├── lqabr.research.audit       -> log_group=audit
        └── lqabr.research.system      -> log_group=system

    Three independent sinks, every call, not three alternatives:

        lqabr.research                 <- ConsoleFormatter + OTLP, propagate=False
        ├── lqabr.research.process     -> research_process.log + log_group=process
        ├── lqabr.research.audit       -> research_audit.log   + log_group=audit
        └── lqabr.research.system      -> research_system.log  + log_group=system

    OTLP export was added ON TOP of this module's existing file behaviour —
    same fixed, undated per-stream files, same deprecated single-file
    `log_file` knob as before. A caller that only ever set `log_dir` keeps
    getting exactly what it got before an exporter existed; the export is
    additional, not a substitute. The files themselves are plain and
    uncapped now — no rotation, no size cap, no day-based sweep — so
    `max_bytes`, `backups` and `retention_days` are accepted only for
    signature parity with the file-sink module and do nothing here.

    **The OTLP handler goes on `lqabr.research`, not on the Python root
    logger.** This is the single thing that makes the difference between
    working and silently doing nothing. This logger sets `propagate = False`
    (a few lines down, and it has to: it is what stops every record being
    printed twice). A handler installed on the root logger — which is what
    `logging.getLogger().addHandler(...)` in the usual OpenTelemetry recipe
    does — is therefore never reached by a single record this agent emits. The
    collector starts, the pipeline is healthy, and nothing arrives. Attaching
    here instead means all three child streams reach the exporter by ordinary
    propagation — the same propagation that already carries them to their
    files, since a file handler sits on the CHILD logger, not the parent.

    The signature is IDENTICAL to the file-sink module's `configure_logging` —
    no OTLP-specific parameters at all — so this stays a genuine drop-in:
    `import ... research_logging_otel as research_logging` and every existing
    call site (agent.py, service_app.py) keeps working unmodified, with the
    exact same file behaviour it always had, now plus OTLP.

    Every OTLP knob is env-only, on purpose: PROJECT_CONTEXT's rule is "config
    is env-driven, a rename outside is a config change, never a code edit",
    and there is exactly one production caller of each of `agent.py` and
    `service_app.py` — a Python-level override parameter would be dead code
    the moment it shipped, reachable only from a test. If a caller-supplied
    override is ever actually needed, add it then, wired to something that
    calls it.

        LQABR_RESEARCH_OTLP_ENDPOINT     collector address  (localhost:4317)
        LQABR_RESEARCH_OTLP_PROTOCOL     grpc | http        (grpc)
        LQABR_RESEARCH_OTLP_INSECURE     1 for a sidecar on localhost   (1)
        LQABR_RESEARCH_OTLP_HEADERS_ENV  the NAME of the var holding the
                                         OTLP headers — never the headers
        LQABR_RESEARCH_OTEL_SERVICE_NAME resource service.name
        LQABR_RESEARCH_OTLP_ENABLED      0 to run console-only

    The standard `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`
    and `OTEL_SERVICE_NAME` are read as fallbacks, so a collector sidecar that
    already injects them needs no LQABR-specific configuration at all.
    """
    if mode:
        set_mode(mode)
    else:
        set_detail(detail)

    root = logging.getLogger("lqabr.research")
    # OURS, by flag — not "is this logger empty?". The file-sink module asks
    # `if not root.handlers`, and any foreign handler on this logger then
    # suppresses the console for the whole process: no narrative on screen,
    # and `propagate` left True so every record also climbs to the root
    # logger and prints a second time. pytest's own capture handler is enough
    # to trigger it, which is how it was found.
    if not any(getattr(existing, "_lqabr_console", False)
               for existing in root.handlers):
        # `console` is stderr for the CLI, whose stdout IS the result document.
        # It must be chosen HERE: the sink's own notes are emitted inside this
        # function, before any caller could redirect a handler.
        handler = _console_handler(log_format, console)
        handler._lqabr_console = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.propagate = False
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Children carry nothing of their own now. Any handler left over from a
    # previous configuration — including a file handler from the other module
    # in a mixed test run — is removed, so a record is exported exactly once.
    for stream in STREAMS:
        child = root.getChild(stream)
        child.setLevel(logging.NOTSET)          # severity is the parent's call
        for existing in list(child.handlers):
            child.removeHandler(existing)

    # Drop any OTLP handler from a previous call before adding a new one:
    # `configure_logging` is idempotent, and two handlers means two exports.
    for existing in list(root.handlers):
        if getattr(existing, "_lqabr_otlp", False):
            root.removeHandler(existing)
            try:
                existing.close()
            except Exception:  # noqa: BLE001
                pass

    _SINK_STATE.update({"dir": log_dir, "files": {}, "degraded": [],
                        "exporter": "none", "endpoint": "", "protocol": "",
                        "service_name": "", "headers_from": ""})
    _SINK_STATE.pop("emit_reported", None)

    # Held, not emitted yet: a note sent before the exporter is attached is a
    # note the collector never sees. Everything below reports at the end,
    # through `_note`, once every sink for this call is in place.
    pending: List[Tuple[str, Dict[str, Any]]] = []

    # ── files — unconditional, exactly the file-sink module's own logic ──────
    # `log_file` (deprecated, one file for all three streams) wins over
    # `log_dir` (one file per stream per day) if both are set, same precedence
    # the file-sink module has always had. Neither one disables OTLP below:
    # they are independent sinks, so both branches fall through rather than
    # returning.
    if log_file:
        handler = _file_handler(log_file, "legacy")
        if handler is not None:
            for stream in STREAMS:
                root.getChild(stream).addHandler(handler)
                _SINK_STATE["files"][stream] = log_file
        pending.append(("log_sink_legacy",
                        {"path": log_file,
                         "detail": "LQABR_RESEARCH_LOG_FILE is deprecated: all "
                                   "three streams share one file. Use "
                                   "LQABR_RESEARCH_LOG_DIR."}))
    elif log_dir:
        # One fixed file per stream — `research_process.log`, no date, no
        # size cap, no rotation. `max_bytes`/`backups`/`retention_days` are
        # kept as parameters only for signature parity with the file-sink
        # module's own `configure_logging()`; none of the three do anything
        # here — there is no cap to hit and no day boundary to sweep.
        for stream in STREAMS:
            path = stream_path(log_dir, stream)
            handler = _file_handler(path, stream)
            if handler is not None:
                root.getChild(stream).addHandler(handler)
                _SINK_STATE["files"][stream] = path

    def _report() -> None:
        for event, fields in pending:
            _note(event, **fields)

    # ── OTLP — the add-on ─────────────────────────────────────────────────
    if not _env_bool("LQABR_RESEARCH_OTLP_ENABLED", True):
        pending.append(("log_export_disabled",
                        {"detail": "LQABR_RESEARCH_OTLP_ENABLED=0; console only"}))
        _report()
        return

    protocol = _env_first("LQABR_RESEARCH_OTLP_PROTOCOL",
                          "OTEL_EXPORTER_OTLP_PROTOCOL", default="grpc").lower()
    protocol = "http" if protocol.startswith("http") else "grpc"
    endpoint = _env_first("LQABR_RESEARCH_OTLP_ENDPOINT",
                          "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
                          "OTEL_EXPORTER_OTLP_ENDPOINT",
                          default=(_OTLP_DEFAULT_HTTP_ENDPOINT if protocol == "http"
                                   else _OTLP_DEFAULT_ENDPOINT))
    insecure = _env_bool("LQABR_RESEARCH_OTLP_INSECURE", True)
    name = _env_first("LQABR_RESEARCH_OTEL_SERVICE_NAME", "OTEL_SERVICE_NAME",
                      default="lqabr-research")
    # The credential by NAME. `headers_env` names the variable; its value is
    # read here and never logged, never returned by `sink_state()`, never put
    # in a record. Only the NAME is reported.
    headers_from = os.environ.get("LQABR_RESEARCH_OTLP_HEADERS_ENV", "")
    headers = os.environ.get(headers_from, "") if headers_from else ""

    handler = _otlp_handler(service_name=name, endpoint=endpoint,
                            protocol=protocol, insecure=insecure,
                            headers=headers, headers_from=headers_from,
                            timeout_seconds=10)
    if handler is None:
        _report()
        return                                   # console only, and said so

    handler._lqabr_otlp = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    pending.append((
        "log_sink_otlp",
        {"endpoint": endpoint, "protocol": protocol, "insecure": insecure,
         "service_name": name, "headers_from": headers_from or "(none)",
         "detail": "attached to lqabr.research, NOT to the root logger — this "
                   "logger does not propagate, so a root handler would never "
                   "see a record"}))
    _report()


def get_obs(run_id: str | None = None, *, refresh: bool = False) -> ResearchLoggingOtel:
    current = _OBS.get()
    if current is None or refresh:
        current = ResearchLoggingOtel(run_id=run_id or new_run_id())
        _OBS.set(current)
    return current
