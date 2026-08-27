"""Audit hooks — the four log streams of Rev 3 FR-7.

Rev 3, "The four log streams":

    audit        Network activity   where the call came from, when it arrived,
                                    which endpoint it went to, response status,
                                    latency, retry count
    process      Gateway activity   the routing decision and the property and
                                    value behind it, events discarded,
                                    protocol conversion, task steps and outcome
    system       Container activity memory and resource consumption, startup and
                                    configuration problems, exceptions
    token/model  Model activity     NOT APPLICABLE to the gateway, which makes
                                    no model calls — recorded as an explicit
                                    exclusion rather than an omission

Every record carries ``run_id`` and (where one exists) ``trigger_id``, so one
lead's path is reconstructible "from log search alone".

Emitted as one JSON object per line to stdout, which is what the agentgateway
sidecar and Cloud Logging both ingest without a shipper. The sidecar emits its
own access log and OTel spans for the same hops; these records are the
gateway's decision-level view, which no proxy can produce because no proxy
knows *why* an agent was chosen.

Append-only: there is no update or delete path in this module by design.

25-Aug-2026: brought to parity with the research/summary agents' own
``log_mode``/``log_dir``/rotation scheme (``research_core/settings.py``), at
Saroja's request ("what the research debug mode have do the same to our
gateway") -- ``terse | normal | debug`` instead of the old
``minimal | standard | verbose`` naming, a configurable log directory instead
of a hardcoded one, size-based rotation on every file sink, and an
``auto | text | json`` console shape (the file is always full JSON regardless
of what the console shows, exactly like research's guarantee). The old
``audit.level`` / ``LQABR_GATEWAY_LOG_LEVEL`` knob still works, mapped onto
the new names, with a one-time deprecation notice on the system stream --
same shape as research's ``log_detail_deprecated``.


26-Aug-2026: the console echo (``console_format=text``/``auto``) went from a
single flat ``key=value`` line to a coloured, glyph-marked one -- the
gateway's own counterpart to research's ``ConsoleFormatter``, built around
the gateway's own event shapes (a network hop in, the routing decision, a
network hop out, the run's closing summary) rather than a copy of research's
(a model call, a step, a campaign lead). The file sink is untouched by this;
it was already, and remains, full JSON on every line regardless of what the
console shows. See ``_GatewayConsole`` below.
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, IO, Iterable, List, Optional, Tuple


class Stream(str, Enum):
    """The four FR-7 streams."""

    AUDIT = "audit"
    PROCESS = "process"
    SYSTEM = "system"
    TOKEN_MODEL = "token_model"


#: The nine lead parameters (Rev 3 Step 6) plus the obvious aliases they
#: arrive under. None of these may ever appear in a gateway log record: the
#: whole point of the design is that no lead-profile data crosses the gateway,
#: and a log line is a way for it to cross.
PROFILE_FIELDS = frozenset({
    "company_id", "company", "industry", "annual_revenue", "revenue",
    # `email_id` is NOT a live field any more (standard `email` only,
    # decided 2026-08-26); it stays in this DENYLIST purely so a legacy
    # payload replayed at the gateway still gets redacted, never logged.
    "frequency_of_purchase", "employee_id", "email_id", "email",
    "phone_number", "phone", "job_title", "decision_maker_name",
    "full_name", "first_name", "last_name", "linkedin_url",
    "location", "address", "company_size_revenue", "lead_profile", "profile",
})

#: Keys that look like profile fields but are legitimate routing metadata.
#: ``propertyName``/``propertyValue`` are explicitly required by FR-7 ("the
#: routing decision together with the property and value it was based on"),
#: and ``decision_maker`` is a property *name*, not a person.
PROFILE_FIELD_ALLOWLIST = frozenset({
    "property_name", "property_value", "propertyName", "propertyValue",
})

#: Mappings under these keys are keyed by *identifier* — an agent key, a
#: discard reason, a stream name — not by field name. The Email agent is called
#: "email"; ``{"agents": {"email": {...}}}`` is a routing table, not a lead's
#: address. Their children's keys are therefore not field names, while their
#: values are still walked, so profile data cannot hide one level deeper.
STRUCTURAL_CONTAINERS = frozenset({
    "agents", "runtime", "routes", "streams", "by_agent", "by_reason",
    "discards_by_reason", "endpoints", "health",
})


#: Value-shaped PII. The key-name guard cannot catch these: a property *value*
#: is config-controlled — put a webhook subscription on a profile property in the
#: portal and `propertyValue` arrives holding an email address, under a key FR-7
#: requires us to log. So values are scrubbed as well as keys.
#:
#: Redacted rather than raised: a portal misconfiguration must not take the
#: ingress down, and the routing decision is still fully reviewable without the
#: value's contents. The record says it was redacted, so nothing is silent.
_EMAILISH = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONEISH = re.compile(
    r"(?<!\w)(?:\+\d[\d\s().-]{7,}\d|\d{3}[\s().-]\d{3}[\s().-]?\d{4})(?!\w)")

REDACTED = "[redacted:looks-like-lead-data]"

#: The three verbosity tiers, matching research/summary's ``log_mode`` naming
#: exactly. ``terse`` = audit stream only (was "minimal"). ``normal`` = all
#: enabled streams, batched discard counts (was "standard"). ``debug`` = all
#: enabled streams, full per-event discard detail (was "verbose"). The value
#: NEVER relaxes the profile-field guard or the redaction below -- same
#: guarantee research states explicitly for its own debug mode.
_MODES = ("terse", "normal", "debug")

#: Old ``audit.level`` / ``LQABR_GATEWAY_LOG_LEVEL`` values, mapped onto the
#: new names so an existing deployment's config keeps working unchanged.
_LEGACY_MODE_ALIASES = {"minimal": "terse", "standard": "normal", "verbose": "debug"}


class ProfileFieldLeak(RuntimeError):
    """A log record tried to carry lead-profile data. Raised, never written.

    Failing loudly is the point: a silent drop would let the trigger-only
    guarantee erode one convenient log line at a time.
    """


def new_run_id() -> str:
    """One run id per inbound request. Correlates all four streams."""
    return f"run-{uuid.uuid4().hex[:16]}"


def _repo_root() -> Path:
    """This file lives at ``agents/gateway/lib/soloai/audit_hooks.py`` --
    four parents up is the repo root, regardless of the process's CWD."""
    return Path(__file__).resolve().parents[4]


def _resolve_log_dir(raw: Optional[str]) -> Path:
    """A relative ``log_dir`` resolves against the repo root, same rule as
    research's ``_resolve_path``. Empty/unset falls back to the gateway's own
    ``logs/agents/gateway`` -- the same convention research/summary use for
    themselves."""
    if not raw:
        return _repo_root() / "logs" / "agents" / "gateway"
    path = Path(raw)
    return path if path.is_absolute() else (_repo_root() / path)


def _resolve_mode(config: Any) -> Tuple[str, bool]:
    """``(mode, came_from_deprecated_knob)``.

    Priority: ``LQABR_GATEWAY_LOG_MODE`` env var, then ``audit.mode`` in
    config.yaml, then the legacy ``LQABR_GATEWAY_LOG_LEVEL`` env var /
    ``audit.level`` config key (remapped, flagged deprecated), then
    ``"normal"``. Same precedence order as research's ``Settings.from_env``.
    """
    env_mode = os.environ.get("LQABR_GATEWAY_LOG_MODE")
    if env_mode:
        mode = env_mode.strip().lower()
        return (mode if mode in _MODES else "normal"), False

    cfg_mode = config.get("audit.mode")
    if cfg_mode:
        mode = str(cfg_mode).strip().lower()
        return (mode if mode in _MODES else "normal"), False

    legacy = os.environ.get("LQABR_GATEWAY_LOG_LEVEL") or config.get("audit.level")
    if legacy:
        return _LEGACY_MODE_ALIASES.get(str(legacy).strip().lower(), "normal"), True

    return "normal", False


def _utc_iso(epoch_seconds: Optional[float] = None) -> str:
    seconds = time.time() if epoch_seconds is None else epoch_seconds
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + \
        f".{int((seconds % 1) * 1000):03d}Z"


# ── console rendering ───────────────────────────────────────────────────────
# Only the CONSOLE is reshaped -- the file sink (above) always gets the full
# JSON line, whatever this renders. Same split research draws between its
# ConsoleFormatter and ``logs/agents/research/*.log``; this is the gateway's
# own version, shaped around the gateway's own event vocabulary -- a network
# hop in, the routing decision behind it, a network hop out, the run's own
# summary line -- rather than a literal port of research's (a model call, a
# step, a campaign lead), since the two agents' events are structurally
# different. 26-Aug-2026, at Saroja's request ("i want the gateway logs to
# be this clear and neat") after seeing research's console output live.

_DIM, _RESET = "\033[2m", "\033[0m"
_RED, _YELLOW, _GREEN = "\033[31m", "\033[33m", "\033[32m"
_STREAM_COLOUR = {"audit": "\033[2m", "process": "\033[36m", "system": "\033[35m"}

#: Same bad/meh vocabulary research's own formatter classifies by, plus the
#: gateway's own words (a route the guard refused to log, an endpoint that
#: never resolved).
_BAD_WORDS = ("failed", "error", "rejected", "unreachable", "unresolved", "leak")
_MEH_WORDS = ("discarded", "deprecated", "suppressed", "degraded", "dropped")

#: Windows consoles default to cp1252, which cannot encode these -- a log
#: line must never be the thing that raises. Chosen per stream, not assumed,
#: same guard research uses for its own console.
_GLYPHS_UNICODE = {"in": "▸", "out": "◂", "ok": "✓", "bad": "✗",
                   "warn": "!", "plain": "·", "cut": "…"}
_GLYPHS_ASCII = {"in": ">", "out": "<", "ok": "+", "bad": "x", "warn": "!",
                 "plain": ".", "cut": "..."}

#: Nothing may wrap -- a wrapped line redraws over its neighbour, which is
#: exactly when a run is busiest under real concurrency.
_MAX_LINE = 165
_FIELD_CHARS = 90

#: Already rendered in the head itself -- never repeated as key=value.
_HEAD_SKIP = ("ts", "stream", "event", "service", "version", "run_id",
             "trigger_id", "direction", "decision")
#: A diagnosis reads last and keeps whatever room is left, rather than being
#: cut in favour of a short bookkeeping field.
_SPILL_KEYS = ("reason", "error", "detail", "note", "basis")

#: Rendered as one network-hop line -- the gateway's equivalent of research's
#: outbound_call/http_out: who, where, status, latency, retries.
_HOP_EVENTS = frozenset({
    "hubspot_ingress_received", "hubspot_ingress_rejected",
    "agent_dispatch", "agent_dispatch_failed",
    "agent_batch_dispatch", "agent_batch_dispatch_failed",
    "vapi_call_report_received", "vapi_call_report_relayed",
    "vapi_call_report_rejected",
})
#: Fields the hop branch already prints explicitly -- excluded from the
#: trailing key=value fields so nothing is shown twice.
_HOP_FIELD_SKIP = _HEAD_SKIP + (
    "agent", "source", "method", "endpoint", "status", "status_code",
    "latency_ms", "retry_count", "batch_size", "event_count")


def _terminal_width(fallback: int = _MAX_LINE) -> int:
    try:
        import shutil
        return max(90, shutil.get_terminal_size(fallback=(fallback, 24)).columns - 1)
    except Exception:  # noqa: BLE001 - width is a nicety, never a failure
        return fallback


def _glyphs_for(stream: Any) -> Dict[str, str]:
    encoding = getattr(stream, "encoding", "") or ""
    try:
        "".join(_GLYPHS_UNICODE.values()).encode(encoding)
    except (LookupError, UnicodeEncodeError, TypeError):
        return _GLYPHS_ASCII
    return _GLYPHS_UNICODE


def _short_run(run_id: Any) -> str:
    """``run-<32 hex>`` is too wide for a line meant to be scanned at a
    glance; the last 8 characters are still enough to grep the request back
    out of the JSON file when two hops need to be tied together."""
    text = str(run_id or "")
    return text[-8:] if len(text) > 8 else (text or "-")


def _console_value(value: Any, cap: int = _FIELD_CHARS, cut: str = "...") -> str:
    """One field, short enough to sit on a terminal line. A nested dict or
    list is summarised (``{a=1, b=2, +3}``) rather than dumped whole -- the
    per-key breakdown still exists in the JSON file this is only a nicety
    for; debug mode routes these to full continuation lines instead, see
    ``_GatewayConsole._fields``."""
    if isinstance(value, dict):
        inner = ", ".join(f"{k}={_console_value(v, 48, cut)}"
                          for k, v in list(value.items())[:6])
        extra = len(value) - 6
        return "{" + inner + (f", +{extra}" if extra > 0 else "") + "}"
    if isinstance(value, (list, tuple)):
        head = ", ".join(str(v) for v in list(value)[:3])
        extra = len(value) - 3
        return f"[{head}{f' +{extra}' if extra > 0 else ''}]"
    text = str(value).replace("\n", " ")
    return text if len(text) <= cap else text[:max(1, cap - len(cut))] + cut


def _classify(event: str, data: Dict[str, Any], g: Dict[str, str]) -> Tuple[str, str]:
    """Glyph + colour for the hop and generic branches, from the event name
    and an explicit error/outcome field -- the same rule research applies to
    its own generic branch."""
    if (data.get("error") or data.get("outcome") == "failed"
            or any(word in event for word in _BAD_WORDS)):
        return g["bad"], _RED
    if any(word in event for word in _MEH_WORDS):
        return g["warn"], _YELLOW
    if event.endswith(("_received", "_resolved", "_relayed", "_ok")) or event in (
            "agent_dispatch", "agent_batch_dispatch", "routing_decision"):
        return g["ok"], _GREEN
    return g["plain"], _STREAM_COLOUR.get(data.get("stream", ""), "")


class _GatewayConsole:
    """One event, one readable line -- the gateway's own counterpart to
    research's ``ConsoleFormatter``, built around a request's actual path
    (a hop in, the decision behind the route, a hop out, the run's own
    summary) instead of research's (a model call, a step, a campaign lead).
    Anything outside that shape still gets a coloured, aligned, glyph-marked
    line via the generic branch -- no event type prints unstyled.
    """

    def __init__(self, colour: bool, glyphs: Dict[str, str], width: int,
                 debug: bool = False) -> None:
        self._colour = colour
        self._g = glyphs
        self._cut = glyphs.get("cut", "...")
        self._width = max(90, int(width))
        self._debug = debug

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self._colour and code else text

    def _fields(self, data: Dict[str, Any], plain_head: str,
               skip: Tuple[str, ...]) -> Tuple[List[str], List[Tuple[str, str]]]:
        """``key=value`` pairs trimmed against the PLAIN width (colour codes
        are invisible on screen but would otherwise eat most of the budget).
        Returns ``(rendered, spill)``; spill is what continues on its own
        indented lines rather than being cut."""
        pairs = [(k, v) for k, v in data.items()
                if k not in skip and v not in ("", None, [], {})]

        if self._debug:
            # Full detail: nothing trimmed, nothing dropped -- a nested dict
            # or list becomes one key per continuation line, same trade
            # research's own debug mode makes.
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
        # A diagnosis reads LAST and keeps whatever room is left, so it is
        # never cut in favour of a short bookkeeping field.
        pairs.sort(key=lambda kv: kv[0] in _SPILL_KEYS)
        rendered: List[str] = []
        spill: List[Tuple[str, str]] = []
        used = 0
        full = False
        for key, value in pairs:
            if key in _SPILL_KEYS:
                text = str(value).replace("\n", " ").strip()
                room = budget - used - len(key) - 2
                if not full and len(text) <= room:
                    rendered.append(f"{self._paint(key, _DIM)}={text}")
                    used += len(key) + 1 + len(text) + 1
                else:
                    spill.append((key, text))
                continue
            if full:
                continue
            piece = f"{key}={_console_value(value)}"
            if used + len(piece) + 1 > budget:
                if used + len(self._cut) + 1 <= budget:
                    rendered.append(self._cut)
                    used += len(self._cut) + 1
                full = True
                continue
            rendered.append(f"{self._paint(key, _DIM)}={_console_value(value)}")
            used += len(piece) + 1
        return rendered, spill

    def _continue(self, line: str, spill: List[Tuple[str, str]], colour: str) -> str:
        for key, text in spill:
            indent = " " * 11
            for chunk in textwrap.wrap(f"{key}: {text}", width=self._width - 11,
                                       subsequent_indent="  ",
                                       break_on_hyphens=False) or [f"{key}:"]:
                line += "\n" + indent + self._paint(chunk, colour)
        return line

    def render(self, record: Dict[str, Any]) -> str:
        ts = record.get("ts", "")
        clock = ts[11:19] if isinstance(ts, str) and len(ts) >= 19 else str(ts)
        event = str(record.get("event", "?"))
        stream = str(record.get("stream", "?"))
        run_tag = _short_run(record.get("run_id"))

        if event in _HOP_EVENTS:
            return self._render_hop(clock, event, record, run_tag)
        if event == "run_summary":
            return self._render_summary(clock, record, run_tag)
        return self._render_generic(clock, event, stream, record, run_tag)

    def _render_hop(self, clock: str, event: str, data: Dict[str, Any],
                    run_tag: str) -> str:
        g = self._g
        inbound = data.get("direction") == "inbound"
        arrow = g["in"] if inbound else g["out"]
        mark, colour = _classify(event, data, g)
        who = str(data.get("agent") or data.get("source") or "?")

        bits: List[str] = []
        method = data.get("method")
        if method:
            bits.append(str(method))
        endpoint = data.get("endpoint")
        if endpoint:
            bits.append(str(endpoint))
        line = " ".join(bits)
        status = data.get("status")
        if status is None:
            status = data.get("status_code")
        if status is not None:
            line += f" {status}"
        latency = data.get("latency_ms")
        if isinstance(latency, (int, float)):
            line += f" {latency:.0f}ms"
        retries = data.get("retry_count")
        if isinstance(retries, int) and retries:
            line += f" (retry {retries})"
        batch = data.get("batch_size")
        if isinstance(batch, int) and batch:
            line += f" x{batch}"
        events = data.get("event_count")
        if isinstance(events, int):
            line += f" events={events}"

        head = (f"{self._paint(clock, _DIM)} {self._paint(arrow, colour)} "
               f"{self._paint(f'{who:<9}', colour)} "
               f"{self._paint(event, _DIM)} {line}".rstrip())
        rendered, spill = self._fields(data, head, _HOP_FIELD_SKIP)
        if rendered:
            head += " " + " ".join(rendered)
        head += f" {self._paint(run_tag, _DIM)}"
        return self._continue(head, spill, colour)

    def _render_summary(self, clock: str, data: Dict[str, Any], run_tag: str) -> str:
        g = self._g
        failed = bool(data.get("dispatched_failed") or data.get("routing_errors"))
        colour = _RED if failed else _GREEN
        mark = g["bad"] if failed else g["ok"]
        counts = " ".join(
            f"{key}={data[key]}" for key in (
                "events_received", "routed", "discarded", "routing_errors",
                "dispatched_ok", "dispatched_failed")
            if isinstance(data.get(key), int))
        duration = data.get("duration_ms")
        tail = f" {duration:.0f}ms" if isinstance(duration, (int, float)) else ""
        return (f"{self._paint(clock, _DIM)} {self._paint(mark, colour)} "
               f"{self._paint('run_summary', colour)} {counts}{tail} "
               f"{self._paint(run_tag, _DIM)}")

    def _render_generic(self, clock: str, event: str, stream: str,
                        data: Dict[str, Any], run_tag: str) -> str:
        g = self._g
        mark, colour = _classify(event, data, g)
        plain = f"{clock} {mark} {event:<26} "
        rendered, spill = self._fields(data, plain, _HEAD_SKIP)
        line = (f"{self._paint(clock, _DIM)} {self._paint(mark, colour)} "
               f"{self._paint(f'{event:<26}', colour)} "
               f"{' '.join(rendered)} {self._paint(run_tag, _DIM)}".rstrip())
        return self._continue(line, spill, colour)



def _console_line(record: Dict[str, Any]) -> str:
    """A short, human-readable rendering of one record -- console (text
    mode) only. The file sink always gets the full JSON line regardless;
    this is purely a nicety for a person watching a local terminal, the
    same split research draws between its console and
    ``logs/agents/research/*.log``."""
    ts = record.get("ts", "")
    clock = ts[11:19] if isinstance(ts, str) and len(ts) >= 19 else str(ts)
    stream = str(record.get("stream", "?"))
    event = str(record.get("event", "?"))
    run_id = record.get("run_id") or "-"
    trigger_id = record.get("trigger_id")
    skip = {"ts", "stream", "event", "service", "version", "run_id", "trigger_id"}
    fields = " ".join(f"{k}={v}" for k, v in record.items()
                      if k not in skip and v not in (None, "", [], {}))
    head = f"{clock} [{stream:<7}] {event:<28} run={run_id}"
    if trigger_id:
        head += f" trig={trigger_id}"
    return f"{head} {fields}".rstrip()


@dataclass
class AuditHooks:
    """Structured writer for the four streams.

    Parameters mirror the ``audit:`` block of ``config.yaml``:

    ``mode``      terse (audit only) | normal (audit+process+system) |
                  debug (adds full discard detail and per-record context) --
                  same three-tier scheme and naming as research/summary's own
                  ``log_mode``. The deprecated ``minimal|standard|verbose``
                  names still work via ``audit.level`` / ``LQABR_GATEWAY_LOG_LEVEL``,
                  resolved in ``from_config``.
    ``streams``   per-stream on/off switches
    ``forbid_profile_fields``  raise ``ProfileFieldLeak`` instead of writing a
                  record that carries any of the nine lead parameters
    ``console_format``  auto (text on a real terminal, nothing extra
                  otherwise) | text | json | off -- console shape only, when
                  ``sink == "file"``. The file itself is always full JSON,
                  whatever this says -- same guarantee research documents for
                  its own agent.log.
    ``log_max_bytes`` / ``log_backups``  size-based rotation, applied to every
                  file this class writes (the main sink and the per-stream
                  secondary logs). ``0`` disables rotation.
    """

    service: str = "agent-gateway"
    version: str = "0.1.0"
    mode: str = "normal"
    #: Set by ``from_config`` when ``mode`` came from the deprecated
    #: ``audit.level`` / ``LQABR_GATEWAY_LOG_LEVEL`` knob rather than the
    #: current ``audit.mode`` / ``LQABR_GATEWAY_LOG_MODE`` one.
    mode_deprecated: bool = False
    streams: Dict[str, bool] = field(default_factory=lambda: {
        Stream.AUDIT.value: True,
        Stream.PROCESS.value: True,
        Stream.SYSTEM.value: True,
        Stream.TOKEN_MODEL.value: False,
    })
    forbid_profile_fields: bool = True
    sink: str = "stdout"
    file_path: Optional[str] = None
    #: auto | text | json | off -- console shape when sink == "file". Ignored
    #: when sink == "stdout" (that IS the console; always JSON there, as
    #: before -- this only controls the EXTRA human-readable echo).
    console_format: str = "auto"
    #: Secondary, single-stream sinks -- mirror the research/summary agents'
    #: own log shape (stream/run_id/event/ts + fields, epoch ts) so
    #: gateway_<stream>.log reads like the other agents' logs. Keyed by
    #: stream value ("process" / "audit" / "system"). Written IN ADDITION to
    #: the normal record above; a stream with no entry here is unaffected.
    secondary_log_paths: Dict[str, str] = field(default_factory=dict)
    #: Bytes before a file rolls over; 0 = never. Same default as research
    #: (~50 MB) and same backup count (5). Applies to the main file sink and
    #: every secondary per-stream file.
    log_max_bytes: int = 52_428_800
    log_backups: int = 5

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _handle: Optional[IO[str]] = field(default=None, repr=False)
    _secondary_handles: Dict[str, IO[str]] = field(default_factory=dict, repr=False)
    #: Lazily built once per instance and cached -- ``mode`` (and so
    #: ``debug``-ness) is fixed for the process's lifetime, and the
    #: terminal/encoding checks it does at construction are cheap but not
    #: free enough to redo on every single emit.
    _console: Optional["_GatewayConsole"] = field(default=None, repr=False)
    #: Test/introspection tap — every record written, in order.
    records: List[Dict[str, Any]] = field(default_factory=list, repr=False)
    keep_records: bool = False

    # ---------------------------------------------------------------- sinks
    def _rotate_if_needed(self, path_str: str, handle: IO[str]) -> IO[str]:
        """Roll ``path_str`` over if ``handle`` has grown past
        ``log_max_bytes``: ``.4`` -> ``.5`` ... ``.1`` -> ``.2``, current file
        -> ``.1``, fresh file opened in its place. ``log_backups == 0``
        just truncates (drops history) rather than growing forever.
        Mirrors ``logging.handlers.RotatingFileHandler`` semantics; done by
        hand because these sinks are plain file handles, not stdlib loggers.
        """
        if self.log_max_bytes <= 0:
            return handle
        try:
            size = handle.tell()
        except (OSError, ValueError):
            return handle
        if size < self.log_max_bytes:
            return handle
        handle.close()
        path = Path(path_str)
        if self.log_backups > 0:
            for i in range(self.log_backups - 1, 0, -1):
                src = Path(f"{path}.{i}")
                dst = Path(f"{path}.{i + 1}")
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)
            backup1 = Path(f"{path}.1")
            if backup1.exists():
                backup1.unlink()
            if path.exists():
                path.rename(backup1)
            return path.open("a", encoding="utf-8")
        # No backups kept -- rotation just means "start this file over".
        return path.open("w", encoding="utf-8")

    def _stream_handle(self) -> IO[str]:
        if self.sink == "file":
            path_str = self.file_path or "./logs/gateway.jsonl"
            if self._handle is None:
                path = Path(path_str)
                path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = path.open("a", encoding="utf-8")  # append-only
            else:
                self._handle = self._rotate_if_needed(path_str, self._handle)
            return self._handle
        return sys.stdout

    def _console_enabled(self) -> bool:
        """Whether to ALSO echo a line to stdout when ``sink == "file"``."""
        if self.console_format == "off":
            return False
        if self.console_format in ("text", "json"):
            return True
        # auto: only when a person is plausibly watching a real terminal.
        try:
            return sys.stdout.isatty()
        except Exception:  # noqa: BLE001 - console niceties never raise
            return False

    def _console_obj(self) -> "_GatewayConsole":
        """The colourised renderer for the human-readable echo -- one per
        instance, built the first time it is needed. Colour follows the
        REAL terminal, independent of ``console_format``: forcing
        ``console_format=text`` on a redirected/piped stdout must not inject
        ANSI codes into a file or CI log, same rule research's own
        ``_console_handler`` applies (``colour=tty``, not ``colour=wants_text``).
        """
        if self._console is None:
            try:
                tty = bool(sys.stdout.isatty())
            except Exception:  # noqa: BLE001 - console niceties never raise
                tty = False
            self._console = _GatewayConsole(
                colour=tty, glyphs=_glyphs_for(sys.stdout),
                width=_terminal_width(), debug=(self.mode == "debug"))
        return self._console

    def _echo_console(self, record: Dict[str, Any], json_line: str) -> None:
        if self.sink != "file" or not self._console_enabled():
            return
        try:
            if self.console_format == "json":
                print(json_line, file=sys.stdout, flush=True)
            else:  # "text", or "auto" resolved true (a real terminal)
                print(self._console_obj().render(record), file=sys.stdout, flush=True)
        except Exception:  # noqa: BLE001 - a console nicety must never break
            try:                                    # a request; fall back to
                print(_console_line(record), file=sys.stdout, flush=True)
            except Exception:  # noqa: BLE001       # the plain one-liner, and
                pass                                # if even that fails, drop it.

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        for _handle in self._secondary_handles.values():
            _handle.close()
        self._secondary_handles.clear()

    # ------------------------------------------------------------ guardrail
    def _assert_no_profile_data(self, payload: Dict[str, Any]) -> None:
        if not self.forbid_profile_fields:
            return
        offenders = sorted(self._walk_for_profile_fields(payload))
        if offenders:
            raise ProfileFieldLeak(
                "refusing to log lead-profile data through the gateway: "
                f"{offenders}. No profile data crosses the gateway (Rev 3, "
                "trigger-only payload); resolve the profile agent-side over MCP."
            )

    @classmethod
    def _walk_for_profile_fields(cls, node: Any, depth: int = 0,
                                 keys_are_identifiers: bool = False) -> Iterable[str]:
        """Yield the name of every key that is a lead-profile field.

        Suppression for structural containers is deliberately narrow: it
        applies to the *immediate* keys of a mapping at the top level of a
        record only, and never propagates through a list or to a deeper level.
        A wider rule was tried and let ``routes=[{"email": …}]`` and
        ``agents={"health":{"email": …}}`` through — the container name would
        re-suppress at any depth, which turns the guard off wherever an
        attacker or a careless caller nests one level further.
        """
        if isinstance(node, dict):
            for key, value in node.items():
                name = str(key)
                if (not keys_are_identifiers
                        and key not in PROFILE_FIELD_ALLOWLIST
                        and name.lower() in PROFILE_FIELDS):
                    yield name
                yield from cls._walk_for_profile_fields(
                    value, depth=depth + 1,
                    keys_are_identifiers=(depth == 0 and name in STRUCTURAL_CONTAINERS))
        elif isinstance(node, list):
            for item in node:
                # Never carry suppression into a list: the items are values,
                # and their keys are field names again.
                yield from cls._walk_for_profile_fields(item, depth=depth + 1,
                                                       keys_are_identifiers=False)

    @classmethod
    def _redact_values(cls, node: Any) -> Any:
        """Scrub email- and phone-shaped text out of string values.

        Returns ``(scrubbed, count)``. Applies everywhere, including inside
        ``property_value`` and free-text ``detail`` / ``error`` fields, which is
        where a lead's address would realistically arrive.
        """
        count = 0
        if isinstance(node, str):
            scrubbed = _EMAILISH.sub(REDACTED, node)
            scrubbed = _PHONEISH.sub(REDACTED, scrubbed)
            return scrubbed, (1 if scrubbed != node else 0)
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                out[key], found = cls._redact_values(value)
                count += found
            return out, count
        if isinstance(node, list):
            out_list = []
            for item in node:
                scrubbed, found = cls._redact_values(item)
                out_list.append(scrubbed)
                count += found
            return out_list, count
        return node, 0

    # -------------------------------------------------------------- writing
    def _enabled(self, stream: Stream) -> bool:
        if not self.streams.get(stream.value, False):
            return False
        if self.mode == "terse":
            return stream is Stream.AUDIT
        return True

    def emit(
        self,
        stream: Stream,
        event: str,
        run_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        **fields: Any,
    ) -> Optional[Dict[str, Any]]:
        """Write one record. Returns it, or ``None`` if the stream is off."""
        record: Dict[str, Any] = {
            "ts": _utc_iso(),
            "stream": stream.value,
            "event": event,
            "service": self.service,
            "version": self.version,
            "run_id": run_id,
            "trigger_id": trigger_id,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        self._assert_no_profile_data(record)
        if self.forbid_profile_fields:
            record, redactions = self._redact_values(record)
            if redactions:
                record["redacted_values"] = redactions

        if not self._enabled(stream):
            return None

        line = json.dumps(record, default=str, separators=(",", ":"))
        with self._lock:
            handle = self._stream_handle()
            handle.write(line + "\n")
            handle.flush()
            self._echo_console(record, line)
            if self.keep_records:
                self.records.append(record)
            secondary_path = self.secondary_log_paths.get(stream.value)
            if secondary_path:
                self._write_secondary_log(stream.value, secondary_path, record)
        return record

    # ------------------------------------------------- secondary stream logs
    def _secondary_log_handle(self, stream_value: str, path_str: str) -> IO[str]:
        handle = self._secondary_handles.get(stream_value)
        if handle is None:
            path = Path(path_str)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8")
        else:
            handle = self._rotate_if_needed(path_str, handle)
        self._secondary_handles[stream_value] = handle
        return handle

    def _write_secondary_log(self, stream_value: str, path_str: str,
                             record: Dict[str, Any]) -> None:
        """Same record, in the research/summary agents' own shape:
        ``{"stream", "run_id", "event", "ts", ...fields}`` with an epoch
        ``ts`` -- so ``gateway_<stream>.log`` reads like the other agents'
        logs instead of the gateway's own service/version-stamped shape.
        Written alongside (not instead of) the record above -- the main
        sink still gets every event, on every stream, as before. Called
        with ``self._lock`` already held by ``emit``.
        """
        extra = {k: v for k, v in record.items()
                if k not in ("ts", "stream", "event", "service", "version", "run_id")}
        secondary = {
            "stream": record["stream"],
            "run_id": record["run_id"],
            "event": record["event"],
            "ts": time.time(),
            **extra,
        }
        line = json.dumps(secondary, default=str, ensure_ascii=False)
        handle = self._secondary_log_handle(stream_value, path_str)
        handle.write(line + "\n")
        handle.flush()

    # ------------------------------------------------- convenience wrappers
    def audit(self, event: str, **kw: Any) -> Optional[Dict[str, Any]]:
        """Network activity: who called, when, which endpoint, status, latency."""
        return self.emit(Stream.AUDIT, event, **kw)

    def process(self, event: str, **kw: Any) -> Optional[Dict[str, Any]]:
        """Gateway activity: the decision and the property/value behind it."""
        return self.emit(Stream.PROCESS, event, **kw)

    def system(self, event: str, **kw: Any) -> Optional[Dict[str, Any]]:
        """Container activity: memory, concurrency, config problems, exceptions."""
        return self.emit(Stream.SYSTEM, event, memory_rss_mb=_rss_mb(), **kw)

    def token_model_exclusion(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Record the token/model stream as N/A — an explicit exclusion.

        Rev 3 is emphatic that this is "recorded as an explicit exclusion
        rather than an omission", so it is written to the system stream at
        startup instead of being silently absent. It is not gated on the
        token_model switch: the switch controls model records, and there are
        none to control.
        """
        record = {
            "ts": _utc_iso(),
            "stream": Stream.SYSTEM.value,
            "event": "token_model_stream_not_applicable",
            "service": self.service,
            "version": self.version,
            "run_id": run_id,
            "trigger_id": None,
            "stream_status": "n/a",
            "reason": (
                "the gateway makes no model calls — routing is a config lookup. "
                "Required of the email and voice agents, which do invoke models."
            ),
        }
        line = json.dumps(record, separators=(",", ":"))
        with self._lock:
            handle = self._stream_handle()
            handle.write(line + "\n")
            handle.flush()
            self._echo_console(record, line)
            if self.keep_records:
                self.records.append(record)
        return record

    # ------------------------------------------------------------ factories
    @classmethod
    def from_config(cls, config: Any, keep_records: bool = False) -> "AuditHooks":
        """Build from a ``lib.soloai.config.Config``."""
        streams = config.section("audit.streams") or {}
        mode, mode_deprecated = _resolve_mode(config)

        log_dir_raw = os.environ.get("LQABR_GATEWAY_LOG_DIR") or config.get("audit.log_dir")
        log_dir = _resolve_log_dir(log_dir_raw)

        hooks = cls(
            service=config.get("gateway.name", "agent-gateway"),
            version=str(config.get("gateway.version", "0.1.0")),
            mode=mode,
            mode_deprecated=mode_deprecated,
            streams={
                Stream.AUDIT.value: bool(streams.get("audit", True)),
                Stream.PROCESS.value: bool(streams.get("process", True)),
                Stream.SYSTEM.value: bool(streams.get("system", True)),
                Stream.TOKEN_MODEL.value: bool(streams.get("token_model", False)),
            },
            forbid_profile_fields=bool(config.get("audit.forbid_profile_fields", True)),
            sink=str(config.get("audit.sink", "stdout")),
            file_path=config.get("audit.file_path"),
            console_format=str(os.environ.get("LQABR_GATEWAY_CONSOLE_FORMAT")
                               or config.get("audit.console_format", "auto")).lower(),
            secondary_log_paths={
                Stream.PROCESS.value: str(log_dir / "gateway_process.log"),
                Stream.AUDIT.value: str(log_dir / "gateway_audit.log"),
                Stream.SYSTEM.value: str(log_dir / "gateway_system.log"),
            },
            log_max_bytes=int(os.environ.get("LQABR_GATEWAY_LOG_MAX_BYTES")
                              or config.get("audit.log_max_bytes", 52_428_800)),
            log_backups=int(os.environ.get("LQABR_GATEWAY_LOG_BACKUPS")
                            or config.get("audit.log_backups", 5)),
            keep_records=keep_records,
        )
        if mode_deprecated:
            hooks.system(
                "audit_mode_deprecated",
                legacy_key="audit.level / LQABR_GATEWAY_LOG_LEVEL",
                mapped_to=mode,
                note=("use audit.mode / LQABR_GATEWAY_LOG_MODE "
                      "(terse|normal|debug) instead"),
            )
        return hooks


def _rss_mb() -> Optional[float]:
    """Container memory in MB, read from /proc — no psutil dependency.

    Rev 3 requires memory and resource consumption on the system stream. The
    light-payload constraint is only "measurable rather than asserted" if the
    number is actually recorded.
    """
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 2)
    except (OSError, IndexError, ValueError):  # pragma: no cover - non-Linux
        return None
