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
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, IO, Iterable, List, Optional


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


class ProfileFieldLeak(RuntimeError):
    """A log record tried to carry lead-profile data. Raised, never written.

    Failing loudly is the point: a silent drop would let the trigger-only
    guarantee erode one convenient log line at a time.
    """


def new_run_id() -> str:
    """One run id per inbound request. Correlates all four streams."""
    return f"run-{uuid.uuid4().hex[:16]}"


def _utc_iso(epoch_seconds: Optional[float] = None) -> str:
    seconds = time.time() if epoch_seconds is None else epoch_seconds
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + \
        f".{int((seconds % 1) * 1000):03d}Z"


@dataclass
class AuditHooks:
    """Structured writer for the four streams.

    Parameters mirror the ``audit:`` block of ``config.yaml``:

    ``level``     minimal (audit only) | standard (audit+process+system) |
                  verbose (adds full discard detail and per-record context)
    ``streams``   per-stream on/off switches
    ``forbid_profile_fields``  raise ``ProfileFieldLeak`` instead of writing a
                  record that carries any of the nine lead parameters
    """

    service: str = "agent-gateway"
    version: str = "0.1.0"
    level: str = "standard"
    streams: Dict[str, bool] = field(default_factory=lambda: {
        Stream.AUDIT.value: True,
        Stream.PROCESS.value: True,
        Stream.SYSTEM.value: True,
        Stream.TOKEN_MODEL.value: False,
    })
    forbid_profile_fields: bool = True
    sink: str = "stdout"
    file_path: Optional[str] = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _handle: Optional[IO[str]] = field(default=None, repr=False)
    #: Test/introspection tap — every record written, in order.
    records: List[Dict[str, Any]] = field(default_factory=list, repr=False)
    keep_records: bool = False

    # ---------------------------------------------------------------- sinks
    def _stream_handle(self) -> IO[str]:
        if self.sink == "file":
            if self._handle is None:
                path = Path(self.file_path or "./logs/gateway.jsonl")
                path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = path.open("a", encoding="utf-8")  # append-only
            return self._handle
        return sys.stdout

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

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
        if self.level == "minimal":
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
            if self.keep_records:
                self.records.append(record)
        return record

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
            if self.keep_records:
                self.records.append(record)
        return record

    # ------------------------------------------------------------ factories
    @classmethod
    def from_config(cls, config: Any, keep_records: bool = False) -> "AuditHooks":
        """Build from a ``lib.soloai.config.Config``."""
        streams = config.section("audit.streams") or {}
        return cls(
            service=config.get("gateway.name", "agent-gateway"),
            version=str(config.get("gateway.version", "0.1.0")),
            level=str(config.get("audit.level", "standard")),
            streams={
                Stream.AUDIT.value: bool(streams.get("audit", True)),
                Stream.PROCESS.value: bool(streams.get("process", True)),
                Stream.SYSTEM.value: bool(streams.get("system", True)),
                Stream.TOKEN_MODEL.value: bool(streams.get("token_model", False)),
            },
            forbid_profile_fields=bool(config.get("audit.forbid_profile_fields", True)),
            sink=str(config.get("audit.sink", "stdout")),
            file_path=config.get("audit.file_path"),
            keep_records=keep_records,
        )


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
