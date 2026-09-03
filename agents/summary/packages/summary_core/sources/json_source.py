"""kind=json — summarise a raw JSON payload of any shape.

No schema is assumed. The payload is optionally narrowed with `select`,
then rendered as stable, sorted, pretty JSON so the model reads the same
document for the same data every time. A title is lifted from the usual
suspects when one is there, and simply left empty when it is not — never
invented.
"""

from __future__ import annotations

from typing import Any, Optional

import requests

from ..summary_logging import SummaryLogging
from ..settings import Settings
from ..types import NormalizedDocument, SourceSpec, json_dumps
from .base import register, select_path, truncate

#: Checked in order. First non-empty string wins.
_TITLE_KEYS = ("title", "name", "subject", "headline", "summary_title", "id")


def title_from(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in _TITLE_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def render(payload: Any) -> str:
    """The document text for a JSON value. A string payload is already text."""
    if isinstance(payload, str):
        return payload
    return json_dumps(payload)


def fetch_json(spec: SourceSpec, settings: Settings, *,
               session: Optional[requests.Session] = None,
               obs: SummaryLogging | None = None) -> NormalizedDocument:
    selected = select_path(spec.payload, spec.select)
    text, was_truncated = truncate(render(selected), settings)
    return NormalizedDocument(
        source_kind="json",
        source_ref=spec.reference,
        title=title_from(selected),
        text=text,
        truncated=was_truncated,
        metadata={
            "select": spec.select or "",
            "python_type": type(selected).__name__,
            "keys": sorted(selected)[:25] if isinstance(selected, dict) else [],
        },
    )


register("json", fetch_json)
