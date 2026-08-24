"""Observability — structured, correlated, and free of secrets.

Three streams, one run_id correlating them (matching the Summary Agent):

    process   what the agent did and why (steps, decisions, counts)
    audit     every hop that left this process (MCP call, model call), with
              endpoint, method, status and duration — never a payload, never
              a credential
    system    startup/shutdown and coarse resource facts

One JSON object per line, to stdout AND (when configured) to the agent's own
log file. `redact()` runs over every field bag on the way out, so a key that
reaches a log call by accident still does not reach the log.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

_SENSITIVE_HINTS = ("token", "secret", "password", "api_key", "apikey",
                    "authorization", "auth", "bearer", "credential")

_MAX_VALUE_CHARS = 500


def new_run_id() -> str:
    return f"res-{uuid.uuid4().hex[:12]}"


def redact(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Blank anything whose NAME suggests a credential; trim long values.

    Name-based, not value-based, on purpose: a token cannot be recognised by
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
        # The JSON string IS the message, so the file handler needs no work.
        # The record rides along on `extra` so the console formatter can render
        # it without parsing its own output back.
        self._logger.info(json.dumps(record, default=repr, ensure_ascii=False),
                          extra={"lqabr_record": record})
        return record


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

    def hop(self, *, service: str, endpoint: str, method: str = "POST",
            status: Optional[int] = None, duration_ms: Optional[float] = None,
            attempt: int = 1, error: str = "") -> None:
        """One outbound call, on the audit stream. Never the payload."""
        self.audit.emit("outbound_call", service=service, endpoint=endpoint,
                        method=method, status=status, duration_ms=duration_ms,
                        attempt=attempt, error=error)


_OBS: Observability | None = None


# ── console rendering ───────────────────────────────────────────────────────
# Only the CONSOLE is reshaped. The log file stays JSON lines, always.

_DIM, _RESET = "\033[2m", "\033[0m"
_STREAM_COLOUR = {"process": "\033[36m", "audit": "\033[2m", "system": "\033[35m"}
_RED, _YELLOW, _GREEN = "\033[31m", "\033[33m", "\033[32m"

_SKIP = ("stream", "run_id", "event", "ts")
#: Console only — the JSON keeps these. `note` on a write is a fixed sentence
#: repeated identically on every lead; on a terminal it is the thing that pushes
#: the line past the wrap point and mangles it.
_CONSOLE_SKIP = ("note",)
#: Nothing may wrap. A wrapped line redraws over its neighbour and the log
#: becomes unreadable exactly when a run is busiest. Measured from the real
#: terminal, with a sane fallback when there isn't one.
_MAX_LINE = 165

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
_GLYPHS_UNICODE = {"call": "→", "ok": "✓", "bad": "✗", "warn": "!", "plain": "·"}
_GLYPHS_ASCII = {"call": "->", "ok": "+", "bad": "x", "warn": "!", "plain": "."}


def _glyphs_for(stream: Any) -> Dict[str, str]:
    encoding = getattr(stream, "encoding", "") or ""
    try:
        "".join(_GLYPHS_UNICODE.values()).encode(encoding)
    except (LookupError, UnicodeEncodeError, TypeError):
        return _GLYPHS_ASCII
    return _GLYPHS_UNICODE


#: Fields that carry the diagnosis. Capping these at the ordinary field width
#: is exactly backwards — they matter most when they are longest. They render
#: LAST and take whatever room is left on the line.
_DIAGNOSTIC = ("reason", "error", "detail")

_FIELD_CHARS = 90


def _value(value: Any, cap: int = _FIELD_CHARS) -> str:
    """One field, short enough to sit on a terminal line."""
    if isinstance(value, (list, tuple)):
        head = ", ".join(str(v) for v in list(value)[:3])
        extra = len(value) - 3
        return f"[{head}{f' +{extra}' if extra > 0 else ''}]"
    text = str(value).replace("\n", " ")
    return text if len(text) <= cap else text[:cap - 1] + "…"


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
        self._g = glyphs or _GLYPHS_ASCII
        self._width = max(90, int(width))

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self._colour else text

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
            body = self._paint(f"{self._g['call']} {data.get('service', '?'):<9} {line}",
                               _DIM)
            if data.get("error"):
                body += " " + self._paint(str(data["error"])[:90], _RED)
            return f"{self._paint(clock, _DIM)} {body}"

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
            line = f"lead {at}/{total}".ljust(12) + f"{data.get('object_id', '')}{tail}"
            out = (f"{self._paint(clock, _DIM)} {self._paint(mark, colour)} "
                   f"{self._paint(line, colour)}")
            if data.get("error"):
                out += " " + self._paint(str(data["error"])[:80], _RED)
            return out

        mark, colour = self._g["plain"], _STREAM_COLOUR.get(stream, "")
        if any(word in event for word in _BAD) or data.get("error"):
            mark, colour = self._g["bad"], _RED
        elif any(word in event for word in _MEH):
            mark, colour = self._g["warn"], _YELLOW
        elif event.endswith(("_ok", "_complete", "_found")):
            mark, colour = self._g["ok"], _GREEN

        pairs = [(k, v) for k, v in data.items()
                 if k not in _SKIP and k not in _CONSOLE_SKIP
                 and v not in ("", None, [], {})]
        # Trim against the PLAIN width, then paint — colour codes are invisible
        # on screen but would otherwise eat most of the budget.
        plain = f"{clock} {mark} {event:<24} "
        budget = self._width - len(plain)
        # A diagnosis reads LAST and keeps whatever room is left, so a long
        # reason is never cut in favour of a short bookkeeping field.
        pairs.sort(key=lambda kv: kv[0] in _DIAGNOSTIC)
        rendered, used, spill = [], 0, []
        for key, value in pairs:
            if key in _DIAGNOSTIC:
                # Never lose a diagnosis to the line width. If it does not fit,
                # it continues on indented lines below — a deliberate wrap,
                # not the accidental kind that redraws over its neighbour.
                text = str(value).replace("\n", " ").strip()
                room = budget - used - len(key) - 2
                if len(text) <= room:
                    rendered.append(f"{self._paint(key, _DIM)}={text}")
                    used += len(key) + 1 + len(text) + 1
                else:
                    spill.append((key, text))
                continue
            piece = f"{key}={_value(value)}"
            if used + len(piece) + 1 > budget:
                rendered.append("…")
                break
            rendered.append(f"{self._paint(key, _DIM)}={_value(value)}")
            used += len(piece) + 1

        line = (f"{self._paint(clock, _DIM)} {self._paint(mark, colour)} "
                f"{self._paint(f'{event:<24}', colour)} "
                f"{' '.join(rendered)}".rstrip())
        for key, text in spill:
            indent = " " * 11
            for chunk in textwrap.wrap(f"{key}: {text}", width=self._width - 11,
                                       subsequent_indent="  ") or [f"{key}:"]:
                line += "\n" + indent + self._paint(chunk, colour)
        return line


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
                      log_format: str = "auto") -> None:
    """Readable console, JSON file. The agent writes its own log, no shell
    redirect. Idempotent across calls."""
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
    global _OBS
    if _OBS is None or refresh:
        _OBS = Observability(run_id=run_id or new_run_id())
    return _OBS
