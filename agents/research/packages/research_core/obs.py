"""Observability — structured, correlated, and free of secrets.

Three streams, one run_id correlating them (matching the Summary Agent):

    process   what the agent did and why (steps, decisions, counts)
    audit     every hop that left this process (MCP call, model call), with
              endpoint, method, status, duration and the PARAMETERS sent —
              never a credential
    system    startup/shutdown and coarse resource facts

One JSON object per line, to stdout AND (when configured) to the agent's own
log file. `redact()` runs over every field bag on the way out — recursively, so
a credential nested inside a call's arguments is blanked too.

**Reading a run.** Every step of the pipeline is framed by a pair:

    ▸ IN   <step>   the inputs that step was handed
    ◂ OUT  <step>   what it produced, and how long it took

and every outbound call carries the parameters it was made with, so "where is
the model called and what did we send it" is answerable from the log alone.
Text payloads (a prompt, a note) appear as a length-marked `*_preview`, never
in full on the console — `preview()` decides how much, `LQABR_RESEARCH_LOG_DETAIL=0`
turns previews and parameter bags off entirely.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import textwrap
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Names that hold a credential VALUE.
_SECRET_VALUE_HINTS = ("token", "secret", "password", "api_key", "apikey",
                       "authorization", "auth", "bearer", "credential")

#: ...but these NAME a credential rather than holding one, and the rule is
#: "log the credential's name, never its value". Blanking the name is the exact
#: inverse: it hid `secret_resolved`'s only field — the line whose whole job is
#: to say WHICH credential came from where — and `secrets_source` in the boot
#: config, which is the first thing you want when a key does not resolve.
_IDENTIFIER_NAMES = ("secret", "credential", "secrets_source")
_IDENTIFIER_SUFFIXES = ("_secret", "_secret_name", "_source", "_ref")

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

#: Detail = the previews and the parameter bags. On by default: a run should be
#: legible without remembering a flag. `LQABR_RESEARCH_LOG_DETAIL=0` (wired
#: through settings) returns the terser shape.
_DETAIL = True


def set_detail(enabled: bool) -> None:
    """Turn payload previews and parameter bags on or off, process-wide."""
    global _DETAIL
    _DETAIL = bool(enabled)


def new_run_id() -> str:
    return f"res-{uuid.uuid4().hex[:12]}"


def preview(text: Any, limit: int = _PREVIEW_CHARS) -> str:
    """A length-marked, single-line sample of a payload.

    Empty when detail is off, so the field simply does not print — the console
    skips empty values and the JSON stays small. ASCII marker on purpose: this
    string reaches a cp1252 Windows console.
    """
    if not _DETAIL:
        return ""
    body = " ".join(str(text or "").split())
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
    if not _DETAIL:
        return {"keys": sorted(str(k) for k in arguments)}
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
        elif isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
            clean[key] = value[:_MAX_VALUE_CHARS] + f"... (+{len(value) - _MAX_VALUE_CHARS} chars)"
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
        # The JSON string IS the message, so the file handler needs no work.
        # The record rides along on `extra` so the console formatter can render
        # it without parsing its own output back.
        self._logger.info(json.dumps(record, default=repr, ensure_ascii=False),
                          extra={"lqabr_record": record})
        return record


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
class Observability:
    """One run's logging handle. Build one per run; pass it down."""

    run_id: str = field(default_factory=new_run_id)
    logger: logging.Logger = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.logger is None:
            self.logger = logging.getLogger("lqabr.research")
        self.process = _Stream("process", self.run_id, self.logger)
        self.audit = _Stream("audit", self.run_id, self.logger)
        self.system = _Stream("system", self.run_id, self.logger)

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
            params: Optional[Dict[str, Any]] = None) -> None:
        """One outbound call, on the audit stream.

        `params` is what we SENT — the tool name and its arguments, the model
        and its knobs — summarised, redacted, never the raw payload and never a
        credential. Without it the log says a call happened but not what it
        asked for, which is the question a person actually has.
        """
        self.audit.emit("outbound_call", service=service, endpoint=endpoint,
                        method=method, status=status, duration_ms=duration_ms,
                        attempt=attempt, error=error,
                        params=(params or {}) if _DETAIL else {})


#: Per-context, NOT per-process. Two campaigns in flight — a webhook and a
#: redelivery — used to fight over one module global, and whichever started
#: last owned it: every lazy `get_obs()` after that, including the route
#: handlers' own audit lines, was stamped with an unrelated run's id.
_OBS: contextvars.ContextVar["Observability | None"] = contextvars.ContextVar(
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
    """One field, short enough to sit on a terminal line."""
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
            for chunk in textwrap.wrap(f"{key}: {text}", width=self._width - 11,
                                       subsequent_indent="  ") or [f"{key}:"]:
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
            head = f"{self._g['call']} {data.get('service', '?'):<9} {line}"
            body = self._paint(head, _DIM)
            # Measured as we go: an error appended blind is how this line used
            # to run off the edge and redraw over its neighbour.
            plain_len = len(clock) + 1 + len(head)
            # WHAT we sent — the tool and its arguments, the model and its knobs.
            sent = data.get("params")
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


def _console_handler(log_format: str) -> logging.Handler:
    """`auto` means: text when a person is watching, JSON when a machine is.

    stdout is what Cloud Logging ingests, so an unattached stdout keeps its
    structured fields — readability never costs production observability.
    """
    handler = logging.StreamHandler(sys.stdout)
    tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    wants_text = log_format == "text" or (log_format == "auto" and tty)
    handler.setFormatter(
        ConsoleFormatter(colour=tty, glyphs=_glyphs_for(sys.stdout),
                         width=_terminal_width()) if wants_text
        else logging.Formatter("%(message)s"))
    return handler


def configure_logging(level: str = "INFO", log_file: str = "",
                      log_format: str = "auto", detail: bool = True) -> None:
    """Readable console, JSON file. The agent writes its own log, no shell
    redirect. Idempotent across calls.

    `detail` carries LQABR_RESEARCH_LOG_DETAIL: payload previews and the
    parameter bag on every outbound call.
    """
    set_detail(detail)
    root = logging.getLogger("lqabr.research")
    fmt = logging.Formatter("%(message)s")
    if not root.handlers:
        root.addHandler(_console_handler(log_format))
        root.propagate = False
    if log_file and not any(isinstance(h, logging.FileHandler)
                            and getattr(h, "_lqabr_path", "") == log_file
                            for h in root.handlers):
        try:
            parent = os.path.dirname(log_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            handler = logging.FileHandler(log_file, encoding="utf-8")
            handler.setFormatter(fmt)
            handler._lqabr_path = log_file  # type: ignore[attr-defined]
            root.addHandler(handler)
        except OSError:
            root.warning(json.dumps({"stream": "system", "event": "log_file_unavailable",
                                     "path": log_file}))
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_obs(run_id: str | None = None, *, refresh: bool = False) -> Observability:
    current = _OBS.get()
    if current is None or refresh:
        current = Observability(run_id=run_id or new_run_id())
        _OBS.set(current)
    return current
