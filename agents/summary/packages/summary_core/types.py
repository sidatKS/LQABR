"""The four shapes the whole agent is written against.

    SourceSpec          what the caller asked us to summarise
    NormalizedDocument  what an adapter turned that into — the ONLY thing
                        the summariser ever sees, so the model behaves
                        identically whatever the input was
    SummaryResult       the validated summary
    WriteResult         what the HubSpot MCP did with it

Plain dataclasses on purpose. `src/schema.py` wraps these in pydantic for
the HTTP surface; the library itself must stay importable in a test that
has no web framework installed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: `scheme://…` in a bare string. Deliberately not just http/https.
_URI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

#: Every input kind the agent accepts. Adding a fifth is a new adapter
#: module plus one registry line — no change to the agent or the API.
SOURCE_KINDS = ("url", "json", "api", "text")


class SourceError(ValueError):
    """Input that cannot be turned into a document. Always carries a reason:
    a bad source is reported, never silently summarised as an empty page."""


@dataclass(frozen=True)
class SourceSpec:
    """What to summarise. Exactly one kind, with the fields that kind needs."""

    kind: str
    url: Optional[str] = None                      # kind=url
    endpoint: Optional[str] = None                 # kind=api
    method: str = "GET"                            # kind=api
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[Any] = None                     # kind=api  (JSON request body)
    payload: Optional[Any] = None                  # kind=json (the data itself)
    select: Optional[str] = None                   # kind=json|api — "$.data.article"
    text: Optional[str] = None                     # kind=text
    options: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise SourceError(
                f"unknown source kind {self.kind!r} (expected one of {', '.join(SOURCE_KINDS)})"
            )
        required = {"url": "url", "api": "endpoint", "text": "text"}.get(self.kind)
        if required and not getattr(self, required):
            raise SourceError(f"source kind {self.kind!r} requires {required!r}")
        if self.kind == "json" and self.payload is None:
            raise SourceError("source kind 'json' requires 'payload'")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceSpec":
        """Build from the wire shape, ignoring keys we do not own.

        A bare string is accepted as a convenience and read as a URL when it
        looks like one, otherwise as text — so `{"source": "https://…"}` and
        the full object both work.
        """
        if isinstance(data, str):
            # Anything carrying a URI scheme is treated as a URL — including
            # `file://` and `ftp://`, which the fetch guard then REFUSES by
            # name. Reading those as prose instead would silently summarise
            # the literal string and hide the caller's mistake.
            if _URI_SCHEME.match(data.strip()):
                return cls(kind="url", url=data.strip())
            return cls(kind="text", text=data)
        if not isinstance(data, dict):
            raise SourceError(f"source must be an object or a string, got {type(data).__name__}")
        known = {f for f in cls.__dataclass_fields__}  # noqa: F821 - dataclass attr
        kind = str(data.get("kind") or "").strip().lower()
        if not kind:
            # Infer, so a caller who sent only a url/payload is not punished.
            if data.get("url"):
                kind = "url"
            elif data.get("endpoint"):
                kind = "api"
            elif data.get("payload") is not None:
                kind = "json"
            elif data.get("text"):
                kind = "text"
        filtered = {k: v for k, v in data.items() if k in known and k != "kind"}
        filtered.setdefault("headers", {})
        filtered.setdefault("options", {})
        return cls(kind=kind, **filtered)

    @property
    def reference(self) -> str:
        """A short, loggable pointer to where this came from. Never a payload —
        an inline JSON body may hold customer data and does not go in a log line."""
        if self.kind == "url":
            return str(self.url)
        if self.kind == "api":
            return f"{self.method.upper()} {self.endpoint}"
        return f"inline:{self.kind}"


@dataclass
class NormalizedDocument:
    """One document, whatever it was before. The summariser's only input."""

    source_kind: str
    source_ref: str
    text: str
    title: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    truncated: bool = False

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["char_count"] = self.char_count
        return data


@dataclass
class SummaryResult:
    """The validated summary. `raw` keeps the model's own words for audit."""

    summary: str
    title: str = ""
    topic: str = ""
    key_points: List[str] = field(default_factory=list)
    concepts: List[str] = field(default_factory=list)
    technologies: List[str] = field(default_factory=list)
    takeaways: List[str] = field(default_factory=list)
    industry: str = ""
    source_kind: str = ""
    source_ref: str = ""
    model: str = ""
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def as_hubspot_text(self, max_chars: int = 60_000) -> str:
        """The single text blob written to the HubSpot summary property.

        HubSpot's multi-line text fields cap at 65 536 characters; we stay
        under it rather than discovering the limit as a 400 at write time.
        """
        lines = []
        if self.title:
            lines.append(self.title)
            lines.append("")
        lines.append(self.summary.strip())
        if self.key_points:
            lines.append("")
            lines.append("Key points:")
            lines.extend(f"- {point}" for point in self.key_points)
        if self.technologies:
            lines.append("")
            lines.append("Technologies: " + ", ".join(self.technologies))
        blob = "\n".join(lines).strip()
        return blob[:max_chars]


@dataclass
class WriteResult:
    """What the MCP did. A failed write never reads as a success."""

    status: str                     # written | dry_run | skipped | error
    object_id: str = ""
    object_type: str = ""
    properties: List[str] = field(default_factory=list)
    tool: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("written", "dry_run", "skipped")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def json_dumps(value: Any, *, indent: int = 2) -> str:
    """JSON for the model to read: stable key order, no ASCII escaping, and
    never an exception — an unserialisable value degrades to its repr."""
    return json.dumps(value, indent=indent, ensure_ascii=False, sort_keys=True, default=repr)
