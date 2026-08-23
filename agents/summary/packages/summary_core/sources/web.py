"""kind=url — fetch a web page and extract its article text.

This is blog-summarizer's `crawl_blog`, moved behind the adapter contract
and given the three things it did not have: an SSRF guard, the house retry
policy, and an audit record per hop.
"""

from __future__ import annotations

from typing import Optional

import requests

from ..obs import Observability
from ..settings import Settings
from ..types import NormalizedDocument, SourceSpec
from . import html as html_extract
from .base import register, request, truncate


def fetch_url(spec: SourceSpec, settings: Settings, *,
              session: Optional[requests.Session] = None,
              obs: Observability | None = None) -> NormalizedDocument:
    response = request(
        "GET", str(spec.url), settings=settings, session=session, obs=obs,
        headers={"User-Agent": html_extract.BROWSER_UA},
    )
    title, text = html_extract.extract(response.text)
    text, was_truncated = truncate(text, settings)
    return NormalizedDocument(
        source_kind="url",
        source_ref=str(spec.url),
        title=title,
        text=text,
        truncated=was_truncated,
        metadata={
            "content_type": response.headers.get("Content-Type", ""),
            "status": response.status_code,
            "final_url": getattr(response, "url", str(spec.url)),
        },
    )


register("url", fetch_url)
