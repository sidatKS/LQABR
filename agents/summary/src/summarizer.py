"""The model call. One document in, one validated SummaryResult out.

Deliberately separate from the ADK agent: the HTTP surface and the CLI use
this directly, so an API caller does not pay for a session handshake and a
tool-choice loop to get a summary. The ADK agent and this path share the
same prompt and the same validation, so they cannot drift into producing
different summaries for the same document.

`completion` is injectable. In production it is `litellm.completion`; in the
tests it is a function, which is why the whole suite runs with no API key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from summary_core.obs import Observability, get_obs
from summary_core.secrets import ensure_provider_credentials
from summary_core.settings import Settings, get_settings
from summary_core.types import NormalizedDocument, SummaryResult

from schema import SUMMARY_FIELDS, SummaryValidationError, parse_summary

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "summarize.md"

#: One retry, with the validator's own complaint fed back. More than one and
#: we are paying for the model to guess; zero and a single stray code fence
#: fails the run.
MAX_MODEL_ATTEMPTS = 2


def build_instruction() -> str:
    """The prompt plus the exact JSON shape, generated from SUMMARY_FIELDS so
    the instruction and the validator can never disagree."""
    prompt = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else ""
    fields = "\n".join(f'  "{name}": {description}' for name, description in SUMMARY_FIELDS.items())
    return f"{prompt}\n\nReturn exactly this JSON object:\n\n{{\n{fields}\n}}\n"


def build_user_message(document: NormalizedDocument) -> str:
    header = [
        f"source_kind: {document.source_kind}",
        f"source: {document.source_ref}",
    ]
    if document.title:
        header.append(f"reported_title: {document.title}")
    if document.truncated:
        header.append("note: this document was truncated at the configured size limit")
    return "\n".join(header) + "\n\n--- document ---\n" + document.text


def _default_completion(**kwargs: Any) -> Any:  # pragma: no cover - needs the network
    from litellm import completion

    return completion(**kwargs)


def _text_of(response: Any) -> str:
    """The assistant's text, from whichever shape the client returned."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return str(message.get("content") or "")
        return ""
    choices = getattr(response, "choices", None) or []
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message is not None else None
        return str(content or "")
    return str(response)


def summarize(document: NormalizedDocument, *,
              settings: Settings | None = None,
              obs: Observability | None = None,
              completion: Optional[Callable[..., Any]] = None) -> SummaryResult:
    """Summarise one document. Raises SummaryValidationError rather than
    returning something the CRM should not receive."""
    settings = settings or get_settings()
    obs = obs or get_obs()
    completion = completion or _default_completion

    resolved = ensure_provider_credentials(settings.model, settings=settings)
    obs.process.emit("model_credentials", model=settings.model,
                     secret_name=resolved.name, source=resolved.source)

    messages = [
        {"role": "system", "content": build_instruction()},
        {"role": "user", "content": build_user_message(document)},
    ]

    last_error = ""
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        obs.process.emit("model_call", model=settings.model, attempt=attempt,
                         chars=document.char_count)
        response = completion(model=settings.model, messages=messages,
                              temperature=settings.temperature)
        raw = _text_of(response)
        try:
            result = parse_summary(raw, source_kind=document.source_kind,
                                   source_ref=document.source_ref, model=settings.model)
        except SummaryValidationError as exc:
            last_error = str(exc)
            obs.process.emit("model_output_invalid", attempt=attempt, reason=last_error)
            if attempt >= MAX_MODEL_ATTEMPTS:
                break
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                    f"That response was rejected: {last_error}. Return ONLY the JSON "
                    "object described above, with no prose and no code fence."},
            ]
            continue

        obs.process.emit("model_output_ok", attempt=attempt, chars=len(result.summary),
                         key_points=len(result.key_points))
        return result

    raise SummaryValidationError(
        f"the model did not return a usable summary after {MAX_MODEL_ATTEMPTS} "
        f"attempts: {last_error}"
    )


def summary_to_json(result: SummaryResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False)
