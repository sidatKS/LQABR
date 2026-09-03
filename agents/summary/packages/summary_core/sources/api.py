"""kind=api — call another HTTP/FastAPI endpoint and summarise what it returns.

This is the "or any data that can be a parameter" case: point the agent at a
service, hand it the headers it needs, optionally select the field that holds
the content, and the rest of the pipeline cannot tell the difference between
that and a blog post.

The response is normalised by its content type — JSON through the json
adapter's renderer, HTML through the same extractor the url adapter uses,
anything else as plain text — so one endpoint returning JSON and another
returning HTML both arrive as a document.
"""

from __future__ import annotations

import json as jsonlib
from typing import Optional

import requests

from ..summary_logging import SummaryLogging
from ..settings import Settings
from ..types import NormalizedDocument, SourceSpec
from . import html as html_extract
from . import json_source
from .base import register, request, select_path, truncate


def fetch_api(spec: SourceSpec, settings: Settings, *,
              session: Optional[requests.Session] = None,
              obs: SummaryLogging | None = None) -> NormalizedDocument:
    response = request(
        spec.method or "GET", str(spec.endpoint), settings=settings,
        session=session, obs=obs, headers=spec.headers, json_body=spec.body,
    )
    content_type = (response.headers.get("Content-Type") or "").lower()

    title = ""
    if "json" in content_type:
        try:
            payload = response.json()
        except (ValueError, jsonlib.JSONDecodeError):
            payload = response.text
        selected = select_path(payload, spec.select)
        title = json_source.title_from(selected)
        text = json_source.render(selected)
    elif "html" in content_type:
        title, text = html_extract.extract(response.text)
    else:
        text = response.text

    text, was_truncated = truncate(text, settings)
    return NormalizedDocument(
        source_kind="api",
        source_ref=spec.reference,
        title=title,
        text=text,
        truncated=was_truncated,
        metadata={
            "content_type": content_type,
            "status": response.status_code,
            "method": (spec.method or "GET").upper(),
            "select": spec.select or "",
        },
    )


register("api", fetch_api)
