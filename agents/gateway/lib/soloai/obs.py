"""obs.py -- the gateway's observability subsystem, in the shape research's
and summary's own ``obs.py`` use, adapted to a gateway that routes HTTP
triggers rather than running a multi-step agent pipeline.

25-Aug-2026 -> 26-Aug-2026: this module absorbs and replaces
``lib/soloai/audit_hooks.py`` (Rev 3 FR-7's original "four log streams"
design) at Saroja's explicit request -- "we want the obs.py in gateway in
place of the audit_hook.py... take the reference of the research [agent]".
Self-contained on purpose: this file imports nothing from ``agents/research``,
``agents/summary``, or any shared package -- same "port it, never import it"
rule research's own ``obs.py`` states for itself, applied here to a third
copy. Only ``.config`` (already local to this package) is imported alongside
the standard library.

Rev 3's four streams, unchanged from ``audit_hooks.py``:

    audit        Network activity   where the call came from, when it arrived,
                                    which endpoint it went to, response status,
                                    latency, retry count
    process      Gateway activity   the routing decision and the property and
                                    value behind it, events discarded,
                                    protocol conversion, task steps and outcome
    system       Container activity memory and resource consumption, startup and
                                    configuration problems, exceptions
    token/model  Model activity     NOT APPLICABLE to the gateway, which makes
                                    no model calls -- recorded as an explicit
                                    exclusion rather than an omission

What ports from research essentially unchanged: the ``terse|normal|debug``
mode axis and its env vars, ``preview()``/``summarize_args()`` as new opt-in
truncation gates, a real ``logging.Logger`` hierarchy (one child logger per
stream) instead of hand-rolled file handles, a colourised ``ConsoleFormatter``
attached as a genuine ``logging.Handler``, and ``_GuardedRotatingFileHandler``
(stdlib ``RotatingFileHandler`` with a swallowed-and-reported Windows
rollover ``PermissionError``) in place of the old hand-rolled rotation.

What stays gateway-only, layered on top rather than relaxed by the port: the
lead-profile guard (``ProfileFieldLeak`` -- raises, never blanks) and the
email/phone value-shaped scrub. Research never touches lead data and never
needed either.

What's deliberately NOT a copy of research, and why: research caches one
``Observability`` per run in a ``contextvars.ContextVar`` because it processes
one campaign at a time per async context. The gateway serves up to 10
concurrent HubSpot requests through a threadpool, so ``run_id`` stays an
explicit parameter threaded through every call -- exactly what the gateway
already does correctly today, and exactly what research's own code comment
warns a shared context would break ("two campaigns in flight... fighting
over one module global"). ``mode`` is still settable per-instance (``Observability(mode=...)``), which
research's own class does not offer -- but every construction now also calls
``set_mode()``, so ``current_mode()`` reflects the same shared, module-global
``_MODE`` research keeps. And the logger itself is no longer a workaround:
earlier in this port ``Observability`` gave every instance a unique
``uuid``-suffixed logger name to dodge Python's global-by-name logger cache,
which was flagged, correctly, as a silent deviation from research's fixed
``"lqabr.research"`` name -- undocumented at the time it was made and not
surfaced until asked. It's fixed: the logger is now the same shared, fixed
name research uses (``"lqabr.gateway"``), and instead of the uuid dodge,
``__post_init__`` clears that shared logger's (and its three children's)
handlers before adding its own -- the same "tear down, then rebuild" idiom
``configure_logging()`` below applies to file handlers on every call, just
triggered at construction time because the gateway's test suite constructs
many ``Observability`` instances directly rather than calling
``configure_logging()`` once up front the way a running service does.

Ported to match research one-for-one, even where the gateway has no caller
for them yet -- because the instruction was to mirror research's ``obs.py``
exactly, not to prune it to today's call sites: ``step()`` / ``Step`` /
``hop()`` (duration-framed IN/OUT logging and outbound-call auditing),
``sink_state()`` (``/health`` sink introspection), ``set_detail()`` (the
deprecated 0/1 mode alias) and the ``STREAMS`` constant. The one adaptation
``step()`` / ``hop()`` need: ``run_id`` is an explicit keyword argument here,
since the gateway threads ``run_id`` per call for threadpool concurrency
rather than baking it into the ``Observability`` the way research's
one-campaign-per-context design does -- the same reason ``run_id`` is
explicit on every other emit in this file.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
import json
import logging
import os
import re
import sys
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, IO, Iterable, List, Optional, Tuple


#: The one shared, fixed logger name every ``Observability`` instance and
#: ``configure_logging()``/``get_obs()`` build on -- research's own
#: equivalent is ``"lqabr.research"``.
_GATEWAY_LOGGER_NAME = "lqabr.gateway"


class Stream(str, Enum):
    """The four FR-7 streams."""

    AUDIT = "audit"
    PROCESS = "process"
    SYSTEM = "system"
    TOKEN_MODEL = "token_model"


#: The three streams that get their own file sink, in the order research names
#: them. token_model has no sink -- it is a single explicit-exclusion record,
#: never a stream of events (see ``token_model_exclusion``). Same constant, and
#: same three names, research's own ``obs.py`` exposes.
STREAMS = ("process", "audit", "system")


# ============================================================== lead-profile
#: The nine lead parameters (Rev 3 Step 6) plus the obvious aliases they
#: arrive under. None of these may ever appear in a gateway log record: the
#: whole point of the design is that no lead-profile data crosses the gateway,
#: and a log line is a way for it to cross. Gateway-only -- research never
#: needs this, since it operates ON lead data by design.
PROFILE_FIELDS = frozenset({
    "company_id", "company", "industry", "annual_revenue", "revenue",
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

#: Mappings under these keys are keyed by *identifier* -- an agent key, a
#: discard reason, a stream name -- not by field name. The Email agent is
#: called "email"; ``{"agents": {"email": {...}}}`` is a routing table, not a
#: lead's address. Their children's keys are therefore not field names, while
#: their values are still walked, so profile data cannot hide one level deeper.
STRUCTURAL_CONTAINERS = frozenset({
    "agents", "runtime", "routes", "streams", "by_agent", "by_reason",
    "discards_by_reason", "endpoints", "health",
})


class ProfileFieldLeak(RuntimeError):
    """A log record tried to carry lead-profile data. Raised, never written.

    Failing loudly is the point: a silent drop would let the trigger-only
    guarantee erode one convenient log line at a time.
    """


def _walk_for_profile_fields(node: Any, depth: int = 0,
                             keys_are_identifiers: bool = False) -> Iterable[str]:
    """Yield the name of every key that is a lead-profile field.

    Suppression for structural containers is deliberately narrow: it applies
    to the *immediate* keys of a mapping at the top level of a record only,
    and never propagates through a list or to a deeper level. A wider rule
    was tried and let ``routes=[{"email": ...}]`` and
    ``agents={"health":{"email": ...}}`` through -- the container name would
    re-suppress at any depth, which turns the guard off wherever a nested key
    happens to be a container name.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            if (not keys_are_identifiers
                    and key not in PROFILE_FIELD_ALLOWLIST
                    and name.lower() in PROFILE_FIELDS):
                yield name
            yield from _walk_for_profile_fields(
                value, depth=depth + 1,
                keys_are_identifiers=(depth == 0 and name in STRUCTURAL_CONTAINERS))
    elif isinstance(node, list):
        for item in node:
            # Never carry suppression into a list: the items are values, and
            # their keys are field names again.
            yield from _walk_for_profile_fields(item, depth=depth + 1,
                                               keys_are_identifiers=False)


# =================================================================== redact
#: Value-shaped PII. The key-name guard cannot catch these: a property
#: *value* is config-controlled -- put a webhook subscription on a profile
#: property in the portal and ``propertyValue`` arrives holding an email
#: address, under a key FR-7 requires us to log. So values are scrubbed as
#: well as keys. Gateway-only -- research never carries lead data at all.
_EMAILISH = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONEISH = re.compile(
    r"(?<!\w)(?:\+\d[\d\s().-]{7,}\d|\d{3}[\s().-]\d{3}[\s().-]?\d{4})(?!\w)")

REDACTED = "[redacted:looks-like-lead-data]"

#: Names that hold a credential VALUE -- new to the gateway, ported from
#: research's own ``obs.py``. ``audit_hooks.py`` never had this: it only
#: caught lead-shaped data, never a generic credential.
_SECRET_VALUE_HINTS = ("token", "secret", "password", "api_key", "apikey",
                       "authorization", "auth", "bearer", "credential")

#: ...but these NAME a credential rather than holding one ("log the
#: credential's name, never its value"), or are a gateway boolean OUTCOME
#: that happens to contain a hint substring ("was the secret verified?") --
#: blanking either is the exact inverse of the rule. Checked verbatim against
#: every field name ``src/audit.py`` actually emits before shipping this list:
#: ``secret_verified`` and ``authenticated`` both contain a hint substring and
#: both are booleans the log exists to show, not values to hide.
_IDENTIFIER_NAMES = ("secret", "credential", "secrets_source")
_IDENTIFIER_SUFFIXES = ("_secret", "_secret_name", "_name", "_source", "_ref")
_SAFE_NAMES = ("secret_verified", "authenticated", "signature_verified",
              "tokens", "max_tokens", "input_tokens", "output_tokens")


def _holds_a_secret(lowered: str) -> bool:
    """True only for a field that carries the credential ITSELF."""
    if lowered in _SAFE_NAMES or lowered in _IDENTIFIER_NAMES:
        return False
    if lowered.endswith(_IDENTIFIER_SUFFIXES):
        return False
    return any(hint in lowered for hint in _SECRET_VALUE_HINTS)


#: Above this many characters a value is trimmed, in terse/normal only --
#: same threshold and same debug-mode exemption as research's own ``redact()``.
_MAX_VALUE_CHARS = 500


def _redact(node: Any, mode: str, _depth: int = 0) -> Tuple[Any, int]:
    """Blank by NAME (credential fields), scrub email/phone-SHAPED values,
    trim long text above ``_MAX_VALUE_CHARS`` (skipped in debug). Recursive to
    depth 4. Returns ``(scrubbed, count)`` so the caller can make redaction
    visible on the record rather than silent.

    Does **not** enforce the lead-profile guard -- that is
    ``_walk_for_profile_fields``, which RAISES, and must run first and
    separately, so a leak is refused rather than silently transformed.
    """
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        count = 0
        for key, value in node.items():
            lowered = str(key).lower()
            if _holds_a_secret(lowered):
                out[key] = "<redacted>" if value else ""
                if value:
                    count += 1
                continue
            if _depth < 4:
                out[key], found = _redact(value, mode, _depth + 1)
                count += found
            else:
                out[key] = value
        return out, count
    if isinstance(node, list):
        items: List[Any] = []
        count = 0
        for item in node:
            if _depth < 4:
                scrubbed, found = _redact(item, mode, _depth + 1)
                items.append(scrubbed)
                count += found
            else:
                items.append(item)
        return items, count
    if isinstance(node, str):
        scrubbed = _EMAILISH.sub(REDACTED, node)
        scrubbed = _PHONEISH.sub(REDACTED, scrubbed)
        count = 1 if scrubbed != node else 0
        if mode != "debug" and len(scrubbed) > _MAX_VALUE_CHARS:
            scrubbed = scrubbed[:_MAX_VALUE_CHARS] + \
                f"... (+{len(scrubbed) - _MAX_VALUE_CHARS} chars)"
        return scrubbed, count
    return node, 0


# ================================================================ mode axis
#: The three verbosity tiers, matching research/summary's ``log_mode`` naming
#: exactly. ``terse`` = audit stream only. ``normal`` = all enabled streams,
#: batched discard counts. ``debug`` = all enabled streams, full per-event
#: discard detail. The value NEVER relaxes the profile-field guard or
#: redaction above -- same guarantee research states for its own debug mode.
#: Settable per-instance (``Observability.mode``) for test convenience, AND
#: mirrored onto the module-global ``_MODE`` below via ``set_mode()`` on every
#: construction -- same shared global research keeps, reached the same way
#: (``set_mode()`` / ``current_mode()``).
_MODES = ("terse", "normal", "debug")

#: The module-global mode, exactly like research's own ``_MODE``. Nothing in
#: this file's redaction/guard path reads it directly (those still take an
#: explicit ``mode`` argument, since ``_write`` always knows the instance's
#: own resolved mode) -- it exists so ``configure_logging()``/``get_obs()``
#: and anything else built against the shared logger, research-style, have
#: the same single source of truth research's own module does.
_MODE = "normal"


def set_mode(mode: str) -> None:
    """Set the shared mode. A bad value is ignored, not raised -- a logging
    setting must never be the reason a run stops. Same rule as research's own
    ``set_mode()``."""
    global _MODE
    if mode in _MODES:
        _MODE = mode


def current_mode() -> str:
    return _MODE


def debugging() -> bool:
    return _MODE == "debug"


def set_detail(enabled: bool) -> None:
    """Deprecated alias for the old boolean knob: 0 -> terse, 1 -> normal.
    Ported verbatim from research so a caller written against research's own
    ``obs.py`` keeps working unchanged against this one."""
    set_mode("normal" if enabled else "terse")

#: Old ``audit.level`` / ``LQABR_GATEWAY_LOG_LEVEL`` values, mapped onto the
#: new names so an existing deployment's config keeps working unchanged.
_LEGACY_MODE_ALIASES = {"minimal": "terse", "standard": "normal", "verbose": "debug"}

#: How much of a text payload a ``preview()`` field carries -- same default
#: as research.
_PREVIEW_CHARS = 240


def new_run_id() -> str:
    """One run id per inbound request. Correlates all four streams."""
    return f"run-{uuid.uuid4().hex[:16]}"


def preview(text: Any, mode: str, limit: int = _PREVIEW_CHARS) -> str:
    """A sample of a payload -- or, in debug, the payload. New to the
    gateway; not yet wired into a call site (see the module docstring).

    terse   ""   the field does not print at all
    normal  a single line, whitespace collapsed, trimmed with a marker
    debug   the value verbatim -- no trim, no marker, no whitespace collapse
    """
    if mode == "terse":
        return ""
    body = str(text or "")
    if mode == "debug":
        return body
    body = " ".join(body.split())
    if not body:
        return ""
    if len(body) <= limit:
        return body
    return f"{body[:limit]}... (+{len(body) - limit} chars)"


def summarize_args(arguments: Any, mode: str, head: int = 60) -> Dict[str, Any]:
    """Call arguments, safe to log: long text becomes ``[N chars] <head>...``.
    New to the gateway; not yet wired into a call site."""
    if not isinstance(arguments, dict):
        return {}
    if mode == "terse":
        return {"keys": sorted(str(k) for k in arguments)}
    if mode == "debug":
        return dict(arguments)
    out: Dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > head:
            out[str(key)] = f"[{len(value)} chars] {' '.join(value.split())[:head]}..."
        else:
            out[str(key)] = value
    return out


def _repo_root() -> Path:
    """This file lives at ``agents/gateway/lib/soloai/obs.py`` -- four
    parents up is the repo root, regardless of the process's CWD."""
    return Path(__file__).resolve().parents[4]


def _resolve_log_dir(raw: Optional[str]) -> Path:
    """A relative ``log_dir`` resolves against the repo root, same rule as
    research's ``_resolve_path``. Empty/unset falls back to the gateway's own
    ``logs/gateway``."""
    if not raw:
        return _repo_root() / "logs" / "gateway"
    path = Path(raw)
    return path if path.is_absolute() else (_repo_root() / path)


def _resolve_mode(config: Any) -> Tuple[str, bool]:
    """``(mode, came_from_deprecated_knob)``.

    Priority: ``LQABR_GATEWAY_LOG_MODE`` env var, then ``audit.mode`` in
    config.yaml, then the legacy ``LQABR_GATEWAY_LOG_LEVEL`` env var /
    ``audit.level`` config key (remapped, flagged deprecated), then
    ``"normal"``.
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


def _rss_mb() -> Optional[float]:
    """Container memory in MB, read from /proc -- no psutil dependency."""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 2)
    except (OSError, IndexError, ValueError):  # pragma: no cover - non-Linux
        return None


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




class _ConsoleFormatter(logging.Formatter):
    """Wraps ``_GatewayConsole`` as a real ``logging.Formatter`` -- the
    difference between this and last turn's version being that it's now a
    genuine part of the logging pipeline (attached to a ``StreamHandler`` in
    ``Observability.__post_init__``) rather than a manual ``print()`` call
    inside ``emit()``. Falls back to the raw message for anything not carrying
    a ``lqabr_record`` (a stray library warning), so nothing is ever swallowed
    -- same rule research's own ``ConsoleFormatter`` follows.
    """

    def __init__(self, renderer: "_GatewayConsole") -> None:
        super().__init__("%(message)s")
        self._renderer = renderer

    def format(self, record: logging.LogRecord) -> str:
        data = getattr(record, "lqabr_record", None)
        if not isinstance(data, dict):
            return record.getMessage()
        try:
            return self._renderer.render(data)
        except Exception:  # noqa: BLE001 - a console nicety must never raise
            return record.getMessage()


class _SecondaryFormatter(logging.Formatter):
    """Research/summary's own JSON shape for ``gateway_<stream>.log``:
    ``{"stream","run_id","event","ts" (epoch float), ...fields}`` -- no
    ``service``/``version`` stamp, epoch ``ts`` instead of the main sink's
    ISO string. Reshaped HERE, at format time, from the same LogRecord the
    main sink and console formatters also see -- one `.info()` call, three
    different renderings, no second write.
    """

    def __init__(self) -> None:
        super().__init__("%(message)s")

    def format(self, record: logging.LogRecord) -> str:
        data = getattr(record, "lqabr_record", None)
        if not isinstance(data, dict):
            return record.getMessage()
        extra = {k: v for k, v in data.items()
                if k not in ("ts", "stream", "event", "service", "version", "run_id")}
        secondary = {
            "stream": data.get("stream"),
            "run_id": data.get("run_id"),
            "event": data.get("event"),
            "ts": time.time(),
            **extra,
        }
        return json.dumps(secondary, default=str, ensure_ascii=False)


# ============================================================== file sinks
class _GuardedRotatingFileHandler(RotatingFileHandler):
    """A rollover that cannot take the process with it.

    These files can live on a Windows filesystem while the gateway process is
    live, and ``doRollover()`` then raises ``PermissionError`` [WinError 32]
    on the rename, uncaught, spamming stderr on every subsequent emit.
    Observability must never kill a request -- so a failed rollover is
    reported ONCE (via ``owner_logger``, bypassing the guard/redact pipeline
    since this message is code-generated, never user data) and the handler
    keeps appending to the file it already has. Ported from research's own
    handler of the same name and the same reasoning.
    """

    def __init__(self, *args: Any, owner_logger: Optional[logging.Logger] = None,
                stream_name: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._owner_logger = owner_logger
        self._stream_name = stream_name
        self._rollover_failed = False

    def doRollover(self) -> None:  # noqa: N802 - logging's own spelling
        if self._rollover_failed:
            return  # already reported; keep appending
        try:
            super().doRollover()
        except OSError as exc:
            self._rollover_failed = True
            if self._owner_logger is not None:
                self._owner_logger.warning(json.dumps({
                    "stream": "system", "event": "log_rotate_failed",
                    "sink": self._stream_name, "path": self.baseFilename,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "detail": "the file is still being appended to; rollover "
                              "is not retried for this handler",
                }))


# ============================================================ step outcome
class Step:
    """What a step produced. Set it before returning; the frame reports it.

    Ported verbatim from research's own ``Step`` -- the outcome object
    ``Observability.step()`` yields. (Research's file defines this class
    twice, a copy-paste slip; here it is defined once.)
    """

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


# =========================================================== the one place
class _Stream:
    """One stream's write path. ``emit()`` and ``__call__`` are the same
    thing -- research's own attribute-style ``obs.audit.emit(event, **kw)``
    and the gateway's existing flat ``hooks.audit(event, **kw)`` call style
    both work against the exact same object, so ``src/audit.py``'s ~25
    existing call sites needed no rewrite beyond the import line.
    """

    def __init__(self, name: str, logger: logging.Logger,
                owner: "Observability") -> None:
        self._name = name
        self._logger = logger
        self._owner = owner

    def emit(self, event: str, run_id: Optional[str] = None,
             trigger_id: Optional[str] = None, **fields: Any) -> Optional[Dict[str, Any]]:
        return self._owner._write(self._name, self._logger, event, run_id,
                                  trigger_id, fields)

    __call__ = emit


@dataclass
class Observability:
    """Structured writer for the four streams -- the gateway's ``obs.py``,
    in place of ``audit_hooks.py``'s ``AuditHooks``. Parameters mirror the
    ``audit:`` block of ``config.yaml``, same names as before this port.

    ``mode``      terse (audit only) | normal (audit+process+system) |
                  debug (adds full discard detail and per-record context) --
                  same three-tier scheme and naming as research/summary's own
                  ``log_mode``. Per-INSTANCE, not a module global -- see the
                  module docstring.
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
                  secondary logs), via stdlib's own ``RotatingFileHandler``
                  now rather than a hand-rolled rotation loop. ``0`` disables
                  rotation (stdlib's own convention).
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
    #: "stdout" | "file" | "none" -- "none" is `get_obs()`'s own lightweight
    #: wrapper: it builds no handlers, relying on `configure_logging()` (or
    #: another `Observability` built with "stdout"/"file") to already have
    #: set up the shared logger.
    sink: str = "stdout"
    file_path: Optional[str] = None
    #: auto | text | json | off -- console shape when sink == "file". Ignored
    #: when sink == "stdout" (that IS the console; always JSON there -- this
    #: only controls the EXTRA human-readable echo).
    console_format: str = "auto"
    #: Secondary, single-stream sinks -- mirror the research/summary agents'
    #: own log shape (stream/run_id/event/ts + fields) so gateway_<stream>.log
    #: reads like the other agents' logs. Keyed by stream value. Written IN
    #: ADDITION to the main sink above; a stream with no entry here is
    #: unaffected.
    secondary_log_paths: Dict[str, str] = field(default_factory=dict)
    log_max_bytes: int = 52_428_800
    log_backups: int = 5

    keep_records: bool = False
    #: Test/introspection tap -- every record actually written, in order.
    #: Populated by ``_write`` directly (not via a logging.Handler) so it
    #: reflects exactly what ``_write`` decided to keep, including the
    #: gating decision, with no risk of a handler seeing a different set.
    records: List[Dict[str, Any]] = field(default_factory=list, repr=False)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, init=False)
    _logger: logging.Logger = field(default=None, repr=False, init=False)  # type: ignore[assignment]
    _stream_loggers: Dict[str, logging.Logger] = field(default_factory=dict, repr=False, init=False)
    _handlers: List[logging.Handler] = field(default_factory=list, repr=False, init=False)

    def __post_init__(self) -> None:
        # A lightweight handle -- exactly research's ``Observability``: it does
        # NOT build any handlers. Handler-building lives in
        # ``configure_logging()``, called once at startup (see server.py's
        # create_app). This object only wraps the shared logger tree with
        # per-stream ``_Stream`` writers. That is the whole point of the
        # research-direction refactor: one place, ``configure_logging()``,
        # owns the sinks; every ``Observability`` (production handle, test
        # instance, or ``get_obs()`` borrow) is just a thin writer over
        # whatever that call set up.
        #
        # Gating (mode / per-stream switches) and the ``.records`` tap in
        # ``_write`` are handler-independent, so a directly-constructed
        # instance with no ``configure_logging()`` behind it still records
        # and still gates correctly -- which is exactly how the test suite
        # reads ``.records`` without any file or console handler in play.
        set_mode(self.mode)  # keep the shared module global in step

        self._logger = logging.getLogger(_GATEWAY_LOGGER_NAME)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

        for name in ("process", "audit", "system"):
            child = self._logger.getChild(name)
            child.setLevel(logging.NOTSET)  # severity is the parent's call
            self._stream_loggers[name] = child

        # Research-style attribute access (`obs.audit.emit(...)`) AND the
        # gateway's existing flat call style (`obs.audit(...)`) both resolve
        # to the same `_Stream.__call__`/`.emit()` -- see `_Stream`.
        self.process = _Stream(Stream.PROCESS.value, self._stream_loggers["process"], self)
        self.audit = _Stream(Stream.AUDIT.value, self._stream_loggers["audit"], self)
        self.system = _Stream(Stream.SYSTEM.value, self._stream_loggers["system"], self)

    def close(self) -> None:
        """Tear down the shared logger's handlers. In production this is the
        gateway's equivalent of research's process-exit cleanup; ``_lifespan``
        calls it on shutdown. Delegates to the module-level ``reset_logging``
        so a handle never has to know which handlers ``configure_logging``
        attached."""
        reset_logging()

    # ------------------------------------------------------------ guardrail
    def _enabled(self, stream_name: str) -> bool:
        if not self.streams.get(stream_name, False):
            return False
        if self.mode == "terse":
            return stream_name == Stream.AUDIT.value
        return True

    # -------------------------------------------------------------- writing
    def _write(self, stream_name: str, logger: logging.Logger, event: str,
              run_id: Optional[str], trigger_id: Optional[str],
              fields: Dict[str, Any], force: bool = False) -> Optional[Dict[str, Any]]:
        """Build one record, guard it, redact it, and -- if the stream is
        enabled -- log it. This is THE place a record is born: `_Stream`,
        `token_model_exclusion`, and nothing else, all funnel through here.

        ``force`` bypasses the per-stream on/off switch (but never the guard
        or the redaction below it) -- used only by `token_model_exclusion`,
        which Rev 3 requires recorded regardless of the system switch.
        """
        record: Dict[str, Any] = {
            "ts": _utc_iso(),
            "stream": stream_name,
            "event": event,
            "service": self.service,
            "version": self.version,
            "run_id": run_id,
            "trigger_id": trigger_id,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        if stream_name == Stream.SYSTEM.value and "memory_rss_mb" not in record:
            # Rev 3 requires memory/resource consumption on the system stream
            # on every record, not just at startup -- same as today's
            # AuditHooks.system() wrapper, now centralised here since `system`
            # is an ordinary `_Stream` rather than its own method. Omitted
            # (not written as null) when unavailable, e.g. non-Linux --
            # same as the None-drop the field update above already applies.
            rss = _rss_mb()
            if rss is not None:
                record["memory_rss_mb"] = rss

        if self.forbid_profile_fields:
            offenders = sorted(_walk_for_profile_fields(record))
            if offenders:
                raise ProfileFieldLeak(
                    "refusing to log lead-profile data through the gateway: "
                    f"{offenders}. No profile data crosses the gateway (Rev 3, "
                    "trigger-only payload); resolve the profile agent-side over MCP."
                )
            record, redactions = _redact(record, self.mode)
            if redactions:
                record["redacted_values"] = redactions

        if not force and not self._enabled(stream_name):
            return None

        line = json.dumps(record, default=str, separators=(",", ":"))
        with self._lock:
            logger.info(line, extra={"lqabr_record": record})
            if self.keep_records:
                self.records.append(record)
        return record

    def token_model_exclusion(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Record the token/model stream as N/A -- an explicit exclusion.

        Rev 3 is emphatic that this is "recorded as an explicit exclusion
        rather than an omission", so it is written to the system stream at
        startup instead of being silently absent. Not gated on the system
        stream's own switch (``force=True``): the switch controls ordinary
        system records, and this one is mandated regardless.
        """
        return self._write(
            Stream.SYSTEM.value, self._stream_loggers["system"],
            "token_model_stream_not_applicable", run_id, None,
            {"stream_status": "n/a",
             "reason": ("the gateway makes no model calls — routing is a "
                       "config lookup. Required of the email and voice "
                       "agents, which do invoke models.")},
            force=True,
        )

    # ---------------------------------------------------------------- steps
    @contextmanager
    def step(self, name: str, *, run_id: Optional[str] = None, **inputs: Any):
        """Frame one step: its inputs, its outputs, its duration -- ported
        from research's own ``step()``.

            with obs.step("resolve_audience", run_id=rid, industry=x) as s:
                leads = resolve(x)
                if not leads:
                    s.skipped("empty_audience")
                    return ...
                s.ok(count=len(leads))

        The frame closes itself -- on the way out of the block, on an early
        return, and on an exception (recorded as the failure it is before it
        propagates). A step that opens can therefore never be left open.

        The one adaptation from research: ``run_id`` is an explicit keyword
        here, because the gateway threads ``run_id`` per call (threadpool
        concurrency) rather than baking it into the ``Observability`` the way
        research's one-campaign-per-context design does. Both step records go
        on the ``process`` stream, same as research.
        """
        self.process.emit("step_in", run_id=run_id, step=name, **inputs)
        outcome = Step()
        started = time.monotonic()
        try:
            yield outcome
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            outcome.failed(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.process.emit("step_out", run_id=run_id, step=name,
                              status=outcome.status,
                              duration_ms=round((time.monotonic() - started) * 1000, 1),
                              **outcome.fields)

    # ---------------------------------------------------------------- hops
    def hop(self, *, service: str, endpoint: str, run_id: Optional[str] = None,
            method: str = "POST", status: Optional[int] = None,
            duration_ms: Optional[float] = None, attempt: int = 1,
            error: str = "", params: Optional[Dict[str, Any]] = None,
            usage: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """One outbound call, on the ``audit`` stream -- ported from research.

        ``params`` is what we SENT (summarised, redacted, never a credential);
        ``usage`` is what the call COST (counts as top-level fields). Research's
        dividing line, unchanged: **audit records what the call cost; process
        records what the call produced.** ``params`` is dropped in terse mode,
        same as research.

        Same ``run_id`` adaptation as ``step()``: explicit keyword here.
        """
        counted = {name: value for name, value in (usage or {}).items()
                   if value is not None}
        return self.audit.emit(
            "outbound_call", run_id=run_id, service=service, endpoint=endpoint,
            method=method, status=status, duration_ms=duration_ms,
            attempt=attempt, error=error,
            params=(params or {}) if self.mode != "terse" else {},
            **counted,
        )

    # ------------------------------------------------------------ factories
    @classmethod
    def from_config(cls, config: Any, keep_records: bool = False) -> "Observability":
        """Build from a ``lib.soloai.config.Config``."""
        streams = config.section("audit.streams") or {}
        mode, mode_deprecated = _resolve_mode(config)

        log_dir_raw = os.environ.get("LQABR_GATEWAY_LOG_DIR") or config.get("audit.log_dir")
        log_dir = _resolve_log_dir(log_dir_raw)

        obs = cls(
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
            obs.system(
                "audit_mode_deprecated",
                legacy_key="audit.level / LQABR_GATEWAY_LOG_LEVEL",
                mapped_to=mode,
                note=("use audit.mode / LQABR_GATEWAY_LOG_MODE "
                      "(terse|normal|debug) instead"),
            )
        return obs


# ===================================================== configure + get_obs
#: Per-process degradation state for the shared-logger path below, in the
#: same shape research's own ``sink_state()`` exposes (not wired to a
#: ``/health`` route yet -- deferred with ``step()``/``hop()``, see the
#: module docstring).
_SINK_STATE: Dict[str, Any] = {"dir": "", "files": {}, "degraded": []}


def sink_state() -> Dict[str, Any]:
    """What the file sinks actually are right now -- for a ``/health`` route.
    Ported verbatim from research. Reflects the most recent
    ``configure_logging()`` call (the shared-logger, research-style path);
    the per-instance ``from_config`` path records its own degradations onto
    the system stream instead, so both are visible."""
    return {"dir": _SINK_STATE["dir"],
            "files": dict(_SINK_STATE["files"]),
            "degraded": list(_SINK_STATE["degraded"])}


def _make_file_handler(path_str, label, max_bytes, backups, owner_logger, formatter):
    """One rotating file handler, or ``None`` with a named reason emitted on
    the system stream. Shared by the main sink and the per-stream secondaries."""
    try:
        parent = os.path.dirname(path_str)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handler = _GuardedRotatingFileHandler(
            path_str, maxBytes=max(0, int(max_bytes)),
            backupCount=max(0, int(backups)), encoding="utf-8",
            delay=True, owner_logger=owner_logger, stream_name=label)
    except OSError as exc:
        _SINK_STATE["degraded"].append(f"{label}:open")
        owner_logger.warning(json.dumps({
            "stream": "system", "event": "log_sink_unavailable", "sink": label,
            "path": path_str, "reason": f"{type(exc).__name__}: {exc}",
            "detail": "the run continues; this stream is console-only",
        }))
        return None
    handler.setFormatter(formatter)
    return handler


def _make_console_handler(console_format):
    """The parent's human-readable echo (sink == "file"), or ``None`` when a
    console is not wanted. ``off`` -> none; ``text``/``json`` -> always; ``auto``
    -> only when a real terminal is attached."""
    if console_format == "off":
        return None
    if console_format == "auto":
        try:
            if not sys.stdout.isatty():
                return None
        except Exception:  # noqa: BLE001 - console niceties never raise
            return None
    handler = logging.StreamHandler(sys.stdout)
    if console_format == "json":
        handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        try:
            tty = bool(sys.stdout.isatty())
        except Exception:  # noqa: BLE001
            tty = False
        renderer = _GatewayConsole(colour=tty, glyphs=_glyphs_for(sys.stdout),
                                   width=_terminal_width(),
                                   debug=(current_mode() == "debug"))
        handler.setFormatter(_ConsoleFormatter(renderer))
    return handler


def _tagged(handler, role):
    handler._lqabr_role = role  # type: ignore[attr-defined]
    return handler


def reset_logging() -> None:
    """Tear down every handler ``configure_logging`` attached to the shared
    logger tree. Called on service shutdown and, in tests, between cases for
    isolation -- the same job research's ``_fresh()`` test helper does."""
    root = logging.getLogger(_GATEWAY_LOGGER_NAME)
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass
    for name in STREAMS:
        child = root.getChild(name)
        for h in list(child.handlers):
            child.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
    _SINK_STATE["dir"] = ""
    _SINK_STATE["files"] = {}
    _SINK_STATE["degraded"] = []


def configure_logging(config: Any = None, *, level: str = "INFO", mode: str = "",
                      log_dir: str = "", sink: str = "stdout",
                      file_path: Optional[str] = None, console_format: str = "auto",
                      secondary_log_paths: Optional[Dict[str, str]] = None,
                      max_bytes: int = 52_428_800, backups: int = 5) -> None:
    """Build the shared logger tree -- the gateway's boot-time logging setup,
    called once at startup exactly the way research's ``configure_logging()``
    is called from its ``lifespan``:

        lqabr.gateway                 <- console echo (sink=file) / stdout (sink=stdout)
        ├── lqabr.gateway.process     -> gateway_process.log
        ├── lqabr.gateway.audit       -> gateway_audit.log
        └── lqabr.gateway.system      -> gateway_system.log
      (+ a combined main sink, gateway.jsonl, on the parent when sink == "file")

    The console/stdout handler on the parent is added exactly once per process
    (research's ``if not root.handlers`` guard); the main sink and the three
    per-stream files are torn down and rebuilt on EVERY call, so re-calling
    with a different ``log_dir`` never leaks a stale handle. This function is
    now the ONE place handlers are built -- ``Observability`` builds none.

    Pass a ``config`` (a ``lib.soloai.config.Config``) to resolve every knob
    from the ``audit:`` block, exactly as ``Observability.from_config`` does,
    so the two never drift. Or pass the explicit keywords for research-style
    direct control (what the tests use against a fresh ``tmp_path``).
    """
    if config is not None:
        m, _ = _resolve_mode(config)
        mode = m
        log_dir = str(_resolve_log_dir(
            os.environ.get("LQABR_GATEWAY_LOG_DIR") or config.get("audit.log_dir")))
        sink = str(config.get("audit.sink", "stdout"))
        file_path = config.get("audit.file_path")
        console_format = str(os.environ.get("LQABR_GATEWAY_CONSOLE_FORMAT")
                             or config.get("audit.console_format", "auto")).lower()
        max_bytes = int(os.environ.get("LQABR_GATEWAY_LOG_MAX_BYTES")
                        or config.get("audit.log_max_bytes", 52_428_800))
        backups = int(os.environ.get("LQABR_GATEWAY_LOG_BACKUPS")
                      or config.get("audit.log_backups", 5))
        secondary_log_paths = {
            "process": os.path.join(log_dir, "gateway_process.log"),
            "audit": os.path.join(log_dir, "gateway_audit.log"),
            "system": os.path.join(log_dir, "gateway_system.log"),
        }

    if mode:
        set_mode(mode)
    root = logging.getLogger(_GATEWAY_LOGGER_NAME)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    # --- parent handlers: console/stdout added once; main sink rebuilt ------
    have_console = any(getattr(h, "_lqabr_role", "") in ("console", "stdout")
                       for h in root.handlers)
    # drop any previous main sink so it can be rebuilt at the (possibly new) path
    for h in [h for h in root.handlers if getattr(h, "_lqabr_role", "") == "main"]:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass

    if sink == "stdout":
        if not have_console:
            h = logging.StreamHandler(sys.stdout)
            h.setFormatter(logging.Formatter("%(message)s"))
            root.addHandler(_tagged(h, "stdout"))
    elif sink == "file":
        if not have_console:
            ch = _make_console_handler(console_format)
            if ch is not None:
                root.addHandler(_tagged(ch, "console"))
        main = _make_file_handler(file_path or "./logs/gateway.jsonl", "main",
                                  max_bytes, backups, root,
                                  logging.Formatter("%(message)s"))
        if main is not None:
            root.addHandler(_tagged(main, "main"))
    # sink == "none": parent gets nothing (a pure get_obs()/handle wrapper)

    # --- children: the three per-stream files, rebuilt every call -----------
    _SINK_STATE["dir"] = log_dir
    _SINK_STATE["files"] = {}
    _SINK_STATE["degraded"] = []
    for name in STREAMS:
        child = root.getChild(name)
        child.setLevel(logging.NOTSET)
        for h in [h for h in child.handlers if getattr(h, "_lqabr_role", "") == "secondary"]:
            child.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
    paths = secondary_log_paths or ({} if not log_dir else {
        name: os.path.join(log_dir, f"gateway_{name}.log") for name in STREAMS})
    for name, path_str in paths.items():
        handler = _make_file_handler(path_str, name, max_bytes, backups, root,
                                     _SecondaryFormatter())
        if handler is not None:
            root.getChild(name).addHandler(_tagged(handler, "secondary"))
            _SINK_STATE["files"][name] = path_str


#: Per-context, not per-process -- the gateway serves concurrent HubSpot
#: requests through a threadpool (see the module docstring on why ``run_id``
#: stays an explicit parameter everywhere else); this cache exists only for a
#: caller that genuinely wants research's ``get_obs()`` convenience for a
#: single current run in its own context.
_OBS: contextvars.ContextVar[Optional["Observability"]] = contextvars.ContextVar(
    "lqabr_gateway_obs", default=None)


def get_obs(*, refresh: bool = False) -> "Observability":
    """A lightweight factory over whatever ``configure_logging()`` already
    set up on the shared logger tree -- builds no handlers of its own, same
    division of labour as research's ``get_obs()``. Cached per async context
    in ``_OBS`` unless ``refresh=True``.

    No ``run_id`` parameter: unlike research, which processes one campaign
    per async context and so keys its cached ``Observability`` by run, the
    gateway serves concurrent HubSpot requests through a threadpool and
    threads ``run_id`` explicitly through every ``.emit()`` call instead (see
    the module docstring). The object this returns is one shared writer over
    the shared logger; callers still pass their own ``run_id`` to each call,
    same as everywhere else in this file.
    """
    current = _OBS.get()
    if current is None or refresh:
        # sink="none" -> a pure handle: __post_init__ builds no handlers, it
        # just wraps whatever configure_logging() already set up. mode is
        # taken from the shared global so gating matches the configured mode.
        current = Observability(mode=current_mode(), sink="none")
        _OBS.set(current)
    return current
