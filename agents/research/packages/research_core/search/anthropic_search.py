"""Web search via Anthropic's server-side tool.

Chosen deliberately over a dedicated search vendor: the agent already holds an
Anthropic credential for the model, so this adds NO new vendor and NO new
secret. The model performs the searches server-side and returns prose with
citations attached, which is exactly the shape a lead-context note needs.

The client is injectable so the whole path is mockable offline — the tests
never reach the network.
"""

from __future__ import annotations

import inspect
import os
import time
from typing import Any, List, Optional

from ..research_logging import (ResearchLogging, debugging, get_obs, preview, redact,
                   summarize_args)
from ..secrets import SecretError, resolve_secret
from ..settings import Settings, get_settings
from ..types import ResearchFindings
from .base import SearchError

#: The server-tool identifier. Config-overridable because tool versions move.
DEFAULT_TOOL_TYPE = "web_search_20250305"


def _strip_provider_prefix(model: str) -> str:
    """`anthropic/claude-sonnet-4-6` -> `claude-sonnet-4-6`.

    The repo's model ids are LiteLLM-shaped (`<provider>/<model>`), but this
    module calls the Anthropic SDK directly, which wants the bare name. Accept
    either spelling so the same config value works in both places.
    """
    text = str(model or "").strip()
    return text.split("/", 1)[1] if "/" in text else text


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """One attribute, whether the reply is an SDK object or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _usage(message: Any) -> dict:
    """What the call actually cost, when the reply says. Never fatal."""
    usage = _field(message, "usage")
    if usage is None:
        return {}
    server = _field(usage, "server_tool_use")
    return {
        "input_tokens": _field(usage, "input_tokens"),
        "output_tokens": _field(usage, "output_tokens"),
        "web_search_requests": _field(server, "web_search_requests") if server else None,
    }


def _supported_kwargs(fn: Any, payload: dict) -> tuple:
    """Keep only the arguments THIS SDK version accepts.

    The Anthropic SDK's ``messages.create`` signature moves between major
    versions — 1.0.0 dropped ``temperature`` outright, and a hard-coded payload
    fails the whole run with a TypeError. Introspecting once and filtering is
    version-proof, and the dropped names are logged rather than swallowed so a
    silently-ignored knob is visible.

    Returns (payload, dropped_names). A signature with **kwargs takes everything.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):          # C-implemented or wrapped
        return payload, []
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return payload, []
    kept = {k: v for k, v in payload.items() if k in params}
    dropped = sorted(set(payload) - set(kept))
    return kept, dropped


class AnthropicWebSearch:
    """Grounded research in one call: search + synthesis, citations returned."""

    name = "anthropic"

    def __init__(self, settings: Settings | None = None, *,
                 client: Any = None, obs: ResearchLogging | None = None,
                 api_key: Optional[str] = None,
                 tool_type: str = DEFAULT_TOOL_TYPE) -> None:
        self._settings = settings or get_settings()
        self._obs = obs or get_obs()
        self._tool_type = os.environ.get("LQABR_RESEARCH_SEARCH_TOOL_TYPE", tool_type).strip()
        self._client = client
        self._api_key = api_key
        self._system_logged = ""

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise SearchError(
                "the anthropic SDK is not installed — add `anthropic` to "
                "agents/research/requirements.txt") from exc
        self._client = anthropic.Anthropic(
            api_key=self._resolve_key(),
            timeout=float(self._settings.search_timeout_seconds))
        return self._client

    def _resolve_key(self) -> str:
        """The model credential, from Secret Manager — same path as HubSpot's.

        Order: an injected key (tests), then ANTHROPIC_API_KEY, then Secret
        Manager by name. The environment variable stays supported because it is
        the SDK's own convention and the fastest local override, but it is
        logged when used, so a run on a stale local key is never silent.
        """
        if self._api_key:
            return self._api_key

        override = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if override:
            self._obs.process.emit(
                "secret_resolved", secret=self._settings.model_token_secret,
                source="env:ANTHROPIC_API_KEY",
                note="environment override — Secret Manager was not consulted")
            return override

        try:
            return resolve_secret(self._settings.model_token_secret,
                                  settings=self._settings, obs=self._obs)
        except SecretError as exc:
            # A missing model credential is a research failure with a reason,
            # not a stack trace: the caller reports it against the lead.
            raise SearchError(
                f"no model credential: {exc}. Set ANTHROPIC_API_KEY, or give "
                f"this process access to the secret.") from exc

    def _tool(self) -> dict:
        tool: dict = {"type": self._tool_type, "name": "web_search",
                      "max_uses": int(self._settings.search_max_uses)}
        if self._settings.search_allowed_domains:
            tool["allowed_domains"] = list(self._settings.search_allowed_domains)
        elif self._settings.search_blocked_domains:
            # The API accepts one or the other, never both.
            tool["blocked_domains"] = list(self._settings.search_blocked_domains)
        return tool

    @staticmethod
    def _collect(message: Any) -> tuple:
        """Text and cited URLs out of the response content blocks.

        Reads defensively: the SDK returns typed objects, the tests hand in
        plain dicts, and a server tool adds block kinds this code does not need
        to understand. Anything unrecognised is skipped, never fatal.
        """
        texts: List[str] = []
        all_texts: List[str] = []
        sources: List[str] = []
        searches = 0

        _get = _field

        for block in (_get(message, "content", []) or []):
            kind = _get(block, "type", "")
            if kind == "text":
                value = _get(block, "text", "") or ""
                if value:
                    texts.append(value)
                    all_texts.append(value)
                for citation in (_get(block, "citations", []) or []):
                    url = _get(citation, "url", "") or ""
                    if url and url not in sources:
                        sources.append(url)
            elif kind in ("server_tool_use", "web_search_tool_use"):
                searches += 1
                # Anything said BEFORE a search is the model narrating its own
                # tool use ("I'll search for ..."), not the note. Drop it.
                texts.clear()
            elif kind == "web_search_tool_result":
                # Some SDK versions emit only the RESULT block, so counting
                # tool_use alone reported 0 searches on a run that clearly made
                # them. Count results too; the pair is halved above.
                searches += 1
                texts.clear()
                for item in (_get(block, "content", []) or []):
                    url = _get(item, "url", "") or ""
                    if url and url not in sources:
                        sources.append(url)
        # A tool_use and its matching result are ONE search.
        searches = max(1, searches // 2) if searches else 0
        # Joined with NO separator: the API splits one sentence into several
        # text blocks wherever a citation attaches, so a blank-line join
        # injected breaks mid-sentence and shredded the prose.
        #
        # `texts` holds only the blocks after the LAST search, which is the
        # model's actual answer. `all_texts` is the fallback for the odd
        # response that ends on a search block and would otherwise be empty.
        return ("".join(texts).strip() or "".join(all_texts).strip(),
                sources, searches)

    def research(self, prompt: str, *, system: str = "") -> ResearchFindings:
        """One grounded pass. Raises SearchError; never returns a fabricated note."""
        settings = self._settings
        model = _strip_provider_prefix(settings.model)
        client = self._ensure_client()
        tool = self._tool()

        payload = {
            "model": model,
            "max_tokens": int(settings.max_tokens),
            "messages": [{"role": "user", "content": prompt}],
        }
        # `search_enabled=0` used to change nothing: the tool was attached
        # regardless, so /health reported `search.enabled: false` while the
        # agent ran (and billed) up to `max_uses` searches per lead.
        if settings.search_enabled:
            payload["tools"] = [tool]
        if system:
            payload["system"] = system

        payload, dropped = _supported_kwargs(client.messages.create, payload)
        if dropped:
            self._obs.process.emit("search_kwargs_dropped", dropped=dropped,
                                   reason="this anthropic SDK version does not "
                                          "accept them; the call proceeds without")

        # WHERE the model is called and WHAT is sent to it. The two payloads —
        # the system prompt and the user prompt — follow as length-marked
        # previews rather than in full: enough to see what was asked, never a
        # 4,000-character block in the middle of a run.
        self._obs.process.emit(
            "model_request", model=model, max_tokens=payload.get("max_tokens"),
            search_enabled=settings.search_enabled,
            search_tool=tool.get("type") if settings.search_enabled else "",
            search_max_uses=tool.get("max_uses") if settings.search_enabled else 0,
            timeout_s=settings.search_timeout_seconds,
            prompt_chars=len(prompt), system_chars=len(system),
            endpoint=f"{self.name}.messages.create", sent_keys=sorted(payload),
            # Only when it differs: the repo's ids are LiteLLM-shaped
            # (`provider/model`) and the SDK wants the bare name.
            model_configured=(settings.model if settings.model != model else ""),
            allowed_domains=tool.get("allowed_domains", []),
            blocked_domains=tool.get("blocked_domains", []),
            # In normal mode: logged in full the first time it is used and on
            # any change, after which `system_chars` is the whole story. In
            # DEBUG the dedup is off — debug means nothing is withheld, and a
            # reader landing on one request must not have to scroll back
            # through the run to find out what the system prompt was.
            system_preview=preview(system)
            if (debugging() or system != self._system_logged) else "",
            prompt_preview=preview(prompt),
            # Debug only: the EXACT dict handed to the SDK, so "what did we
            # send" has one answer instead of being reassembled from
            # `sent_keys` plus two previews. Redacted like any other field.
            payload=redact(payload) if debugging() else {})
        self._system_logged = system

        sent = summarize_args({"model": model,
                               "max_tokens": payload.get("max_tokens"),
                               "tool": tool.get("type") if settings.search_enabled else "none",
                               "max_uses": tool.get("max_uses") if settings.search_enabled else 0,
                               "prompt_chars": len(prompt),
                               "system_chars": len(system)})

        started = time.monotonic()
        try:
            message = client.messages.create(**payload)
        except Exception as exc:  # noqa: BLE001 - surfaced as SearchError with the step named
            duration = round((time.monotonic() - started) * 1000, 1)
            self._obs.hop(service="anthropic", endpoint="messages.create",
                          error=str(exc), duration_ms=duration, params=sent)
            raise SearchError(f"the web-search model call failed "
                              f"({type(exc).__name__}): {exc}") from exc

        duration = round((time.monotonic() - started) * 1000, 1)
        # Extracted BEFORE the hop is written, so the meter rides the line for
        # the call it measures. It used to run four lines below, which is why
        # answering "what did that call cost" meant joining audit to process on
        # run_id and ordering.
        usage = _usage(message)
        self._obs.hop(service="anthropic", endpoint="messages.create",
                      status=200, duration_ms=duration, params=sent, usage=usage)

        text, sources, searches = self._collect(message)
        # The reply, before it is judged: token cost, why it stopped, how many
        # searches it really ran, and the head of what it wrote back.
        self._obs.process.emit(
            "model_response", model=model, duration_ms=duration,
            stop_reason=_field(message, "stop_reason", "") or "",
            # Kept on process for ONE release alongside the new audit fields.
            # Pulling a field out of a line something may already read is the
            # same class of change as re-homing service_start — migrate, don't
            # yank. Remove these three in the release after this one.
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            web_search_requests=usage.get("web_search_requests"),
            searches=searches, sources=len(sources), source_urls=sources,
            chars=len(text),
            text_preview=preview(text))
        if not text:
            raise SearchError("the model returned no usable text for the research pass")

        return ResearchFindings(text=text, sources=sources, searches=searches, model=model)
