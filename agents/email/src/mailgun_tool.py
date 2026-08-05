"""The Mailgun tool call — "give me the status of these track IDs".

Local to the email agent (this account owns Mailgun, so there is no separate
service and no externally hosted tool folder). When the agent needs the
current state of messages it has already sent, it makes ONE outbound tool
call to Mailgun's events API from inside the run — it does not stand up a
listener and it does not run a scheduler.

`GET /v3/{domain}/events` is Mailgun's own record of what happened to every
message it accepted. The event objects it returns are the *same shape*
Mailgun would push to a webhook — `event`, `severity`,
`message.headers.message-id`, `user-variables`, `id`, `timestamp` — so
`events.py::handle_event` consumes either without knowing which it got, and
push and pull share one implementation of steps 8-9.

ONE CALL PER RUN. NO SCHEDULER. Every function here is called once, on
demand, inside a run a trigger already started; adding a background thread,
a `while True`, or a scheduled wake-up would defeat the design.

Observability: the caller passes a sink with `.audit()` / `.process()`
(the agent's `observability.MCPObservability`); absent one, it is a no-op.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional, Protocol, runtime_checkable

import requests

from lqabr_core.mailgun import MailgunError
from lqabr_core.secrets import get_secret

API_BASE = os.environ.get("MAILGUN_API_BASE", "https://api.mailgun.net/v3")

#: Mailgun caps a single events page at 300.
_PAGE_MAX = 300
_RETRYABLE = (429, 500, 502, 503, 504)

#: Hard ceiling on pages walked in one tool call. A run sends a bounded batch
#: (LQABR_EMAIL_BATCH_LIMIT, default 25), so anything past this means the
#: filter is wrong — stop rather than walk Mailgun's whole history and burn
#: the container's time budget.
_MAX_PAGES = 20


@runtime_checkable
class ObservabilitySink(Protocol):
    """What this tool needs from a logger: two structured sinks. The agent's
    ``observability.MCPObservability`` satisfies it; so does the no-op below."""

    def audit(self, **fields: Any) -> None: ...
    def process(self, **fields: Any) -> None: ...


class NullObservability:
    """Default no-op sink so a caller can pass nothing and reach no network
    logging path by accident."""

    def audit(self, **fields: Any) -> None:  # noqa: D401 - trivial no-op
        return None

    def process(self, **fields: Any) -> None:
        return None


class MailgunEventsClient:
    """Typed, mockable. Inject a `requests.Session` in tests; nothing here
    reaches the network unless a real session is supplied."""

    def __init__(self, api_key: Optional[str] = None, domain: Optional[str] = None,
                 session: Optional[requests.Session] = None,
                 obs: Optional[ObservabilitySink] = None,
                 max_retries: int = 3, backoff_seconds: float = 1.0) -> None:
        self._api_key = api_key or get_secret("lqabr-mailgun-api-key")
        self._domain = domain or os.environ.get("MAILGUN_DOMAIN", "")
        if not self._domain:
            raise MailgunError("MAILGUN_DOMAIN is not configured")
        self._session = session or requests.Session()
        self._obs = obs or NullObservability()
        self._max_retries = max_retries
        self._backoff = backoff_seconds

    # ------------------------------------------------------------------ http
    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One audited Mailgun hop. Same retry contract as everything else in
        this repo: 3 tries, exponential backoff on 429/5xx, and a
        MailgunError on final failure rather than a silent empty result."""
        endpoint = url.replace(API_BASE, "mailgun:")
        last_error: Optional[str] = None

        for attempt in range(self._max_retries):
            try:
                resp = self._session.get(url, auth=("api", self._api_key),
                                         params=params, timeout=30)
            # `requests.RequestException` is a SUBCLASS of OSError, not a
            # superset: a plain OSError raised below the requests layer
            # (a missing/unreadable TLS CA bundle, socket exhaustion, a
            # DNS failure surfacing from the OS) is NOT caught by
            # `except RequestException` and would escape this retry loop
            # as an unhandled 500. Catch both.
            except (requests.RequestException, OSError) as exc:
                last_error = str(exc)
                self._obs.audit(step=8, direction="outbound", endpoint=endpoint,
                                method="GET", status_code=None, attempt=attempt + 1,
                                error=last_error)
            else:
                self._obs.audit(step=8, direction="outbound", endpoint=endpoint,
                                method="GET", status_code=resp.status_code,
                                attempt=attempt + 1)
                if resp.status_code == 200:
                    return resp.json() if resp.text else {}
                if resp.status_code in _RETRYABLE:
                    last_error = f"HTTP {resp.status_code}"
                else:
                    raise MailgunError(
                        f"Mailgun GET {endpoint} failed: HTTP {resp.status_code}: "
                        f"{resp.text[:300]}")
            time.sleep(self._backoff * (2 ** attempt))

        raise MailgunError(
            f"Mailgun GET {endpoint} failed after {self._max_retries} retries: {last_error}")

    # ------------------------------------------------------------- the tool
    def fetch_events(self, message_ids: Iterable[str] = (),
                     correlation_token: str = "",
                     begin: Optional[str] = None,
                     limit: int = _PAGE_MAX) -> List[Dict[str, Any]]:
        """Every event Mailgun holds for this run's messages.

        Mailgun's events API filters only by its own documented fields; a
        custom user variable such as `v:lqabr_correlation_token` is NOT one of
        them, and sending it as a query parameter is rejected with HTTP 400
        ("Unknown parameter"). The run is therefore matched CLIENT-SIDE — by
        the message ids step 7 persisted and by the correlation token the
        events echo back. When exactly one message id is wanted we also narrow
        server-side with Mailgun's supported `message-id` filter, so the
        response holds just that message's events.

        `ascending=yes` is sent ONLY alongside a `begin` anchor. On its own it
        makes Mailgun search forward from *now* and return zero events for a
        message already sent — so the default direction (most-recent-first) is
        used instead. Order does not matter downstream: `resolve_status` ranks
        by precedence, not arrival.

        Returns the raw event objects, ready for `handle_event`."""
        wanted = {m.strip("<>") for m in message_ids if m}
        params: Dict[str, Any] = {"limit": min(limit, _PAGE_MAX)}
        # `message-id` is a documented events filter; a user variable is not.
        if len(wanted) == 1:
            params["message-id"] = next(iter(wanted))
        if begin:
            params["begin"] = begin
            params["ascending"] = "yes"   # only meaningful with a begin anchor

        url = f"{API_BASE}/{self._domain}/events"
        collected: List[Dict[str, Any]] = []
        pages = 0

        while url and pages < _MAX_PAGES:
            body = self._get(url, params if pages == 0 else None)
            items = body.get("items", []) or []
            collected.extend(items)
            pages += 1
            url = ((body.get("paging") or {}).get("next") or "")
            if not items:
                break

        # Keep only this run's events: a stored message id, or — as a fallback
        # when id formatting drifts — the correlation token the send tagged.
        if wanted or correlation_token:
            collected = [e for e in collected
                         if _message_id_of(e).strip("<>") in wanted
                         or (correlation_token and _correlation_of(e) == correlation_token)]

        self._obs.process(step=8, event="mailgun_events_fetched",
                          correlation_token=correlation_token or None,
                          message_ids=len(wanted) or None,
                          events=len(collected), pages=pages)
        return collected


def _message_id_of(event: Dict[str, Any]) -> str:
    message = event.get("message") or {}
    headers = message.get("headers") or {}
    return str(headers.get("message-id") or message.get("id") or "")


def _correlation_of(event: Dict[str, Any]) -> str:
    variables = event.get("user-variables") or event.get("user_variables") or {}
    return str(variables.get("lqabr_correlation_token") or "")


def fetch_event_status(message_ids: Iterable[str] = (), correlation_token: str = "",
                       client: Optional[MailgunEventsClient] = None,
                       obs: Optional[ObservabilitySink] = None,
                       **kwargs: Any) -> List[Dict[str, Any]]:
    """The tool entry point: hand it the track IDs from a run and get their
    status back."""
    events_client = client or MailgunEventsClient(obs=obs, **kwargs)
    return events_client.fetch_events(message_ids=message_ids,
                                      correlation_token=correlation_token)


#: The named tool surface.
TOOLS = {"fetch_event_status": fetch_event_status}
