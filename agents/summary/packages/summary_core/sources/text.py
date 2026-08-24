"""kind=text — already a document. Capped, never re-parsed."""

from __future__ import annotations

from typing import Optional

import requests

from ..obs import Observability
from ..settings import Settings
from ..types import NormalizedDocument, SourceSpec
from .base import register, truncate


def fetch_text(spec: SourceSpec, settings: Settings, *,
               session: Optional[requests.Session] = None,
               obs: Observability | None = None) -> NormalizedDocument:
    text, was_truncated = truncate(str(spec.text or ""), settings)
    return NormalizedDocument(
        source_kind="text",
        source_ref=spec.reference,
        title=str(spec.options.get("title", "")),
        text=text,
        truncated=was_truncated,
    )


register("text", fetch_text)
