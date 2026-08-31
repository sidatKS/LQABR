"""Mailgun client — outbound email + webhook signature verification.

OWNED BY THE EMAIL AGENT. Moved out of `packages/lqabr_core` on 2026-08-26:
nothing else in the repo ever imported it, and a transport only this agent
speaks does not belong in a package shared with research, summary, text_voice,
lead_profile and the gateway. `get_secret` stays in lqabr_core — secret
resolution genuinely is shared.

Secrets (Secret Manager): lqabr-mailgun-api-key, lqabr-mailgun-webhook-signing-key.
Config (env): MAILGUN_DOMAIN, MAILGUN_FROM (e.g. "LQABR <outreach@mg.example.com>").
  MAILGUN_FROM must be on MAILGUN_DOMAIN so the From aligns with DKIM/the
  envelope sender — a From on a different domain makes clients show the VERP
  bounce address and "on behalf of". Optional MAILGUN_REPLY_TO routes human
  replies to a mailbox on any domain without breaking that alignment.

Tracking: messages are sent with opens/clicks tracking enabled; Mailgun then
POSTs `delivered` / `opened` / `clicked` events to `POST /mailgun/events` on
this same service (service_app.py -> events.py), which records them on the
HubSpot contact.

NO RUN STATE: `send_email(variables=...)` is what carries `lqabr_object_id`
onto the message. Mailgun echoes it back on every event it raises, which is
how an event days later is attributed to its lead on a container that scaled
to zero in between. Never drop that variable.

Typed and mockable — inject a requests.Session in tests.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any, Dict, Optional

import requests

from lqabr_core.secrets import get_secret

API_BASE = "https://api.mailgun.net/v3"


class MailgunError(RuntimeError):
    pass


class MailgunClient:
    def __init__(self, api_key: Optional[str] = None, domain: Optional[str] = None,
                 sender: Optional[str] = None, session: Optional[requests.Session] = None,
                 max_retries: int = 3, reply_to: Optional[str] = None) -> None:
        self._api_key = api_key or get_secret("lqabr-mailgun-api-key")
        self._domain = domain or os.environ.get("MAILGUN_DOMAIN", "")
        if not self._domain:
            raise MailgunError("MAILGUN_DOMAIN is not configured")
        self._sender = sender or os.environ.get("MAILGUN_FROM", f"LQABR <outreach@{self._domain}>")
        # Keep From on the sending domain so it aligns with DKIM/the envelope
        # (no "on behalf of"), but let human replies land in a real mailbox on
        # any domain. Empty/unset -> no Reply-To header, replies go to From.
        self._reply_to = (reply_to if reply_to is not None
                          else os.environ.get("MAILGUN_REPLY_TO", "")).strip()
        self._session = session or requests.Session()
        # NOTE: send_one() constructs this with max_retries=1 deliberately.
        # A timeout means "no reply", not "not sent" — Mailgun may have
        # accepted the message and answered late, so retrying that ambiguity
        # is how one lead received the same email two or three times.
        self._max_retries = max_retries

    def config(self) -> Dict[str, Any]:
        """What __init__ actually resolved — safe to log, and worth logging:
        the constructor reads a secret and three env vars, any of which can be
        wrong in a way that only shows up as a failed send.

        The api key is FINGERPRINTED, never returned."""
        return {
            "domain": self._domain,
            "sender": self._sender,
            "reply_to": self._reply_to or None,
            "max_retries": self._max_retries,
            "api_key_fingerprint": hashlib.sha256(self._api_key.encode()).hexdigest()[:12],
        }

    def send_email(self, to: str, subject: str, html: str, text: Optional[str] = None,
                   tags: Optional[list[str]] = None,
                   variables: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Send one tracked email. `variables` are attached as Mailgun user
        variables and echoed back in webhook events — the agent always attaches
        `lqabr_object_id` so an event can be tied back to its lead."""
        data: Dict[str, Any] = {
            "from": self._sender,
            "to": to,
            "subject": subject,
            "html": html,
            "o:tracking": "yes",
            "o:tracking-opens": "yes",
            "o:tracking-clicks": "yes",
        }
        if self._reply_to:
            # Mailgun passes arbitrary MIME headers via the `h:` prefix.
            data["h:Reply-To"] = self._reply_to
        if text:
            data["text"] = text
        for tag in tags or []:
            data.setdefault("o:tag", [])
            data["o:tag"].append(tag)
        for key, value in (variables or {}).items():
            data[f"v:{key}"] = value

        last: Optional[str] = None
        for attempt in range(self._max_retries):
            try:
                resp = self._session.post(f"{API_BASE}/{self._domain}/messages",
                                          auth=("api", self._api_key), data=data, timeout=30)
            # `requests.RequestException` is a SUBCLASS of OSError, not a
            # superset: a plain OSError raised below the requests layer
            # (a missing/unreadable TLS CA bundle, socket exhaustion, a
            # DNS failure surfacing from the OS) is NOT caught by
            # `except RequestException` and would escape this retry loop
            # as an unhandled 500. Catch both.
            except (requests.RequestException, OSError) as exc:
                last = str(exc)
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {resp.status_code}"
                else:
                    raise MailgunError(f"Mailgun send failed: HTTP {resp.status_code}: {resp.text[:300]}")
            time.sleep(2 ** attempt)
        raise MailgunError(f"Mailgun send failed after {self._max_retries} retries: {last}")


def verify_webhook_signature(timestamp: str, token: str, signature: str,
                             signing_key: Optional[str] = None) -> bool:
    """Verify a Mailgun webhook payload (HMAC-SHA256 of timestamp+token).

    compare_digest, not ==, so the comparison is constant-time."""
    key = signing_key or get_secret("lqabr-mailgun-webhook-signing-key")
    expected = hmac.new(key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


__all__ = ["MailgunClient", "MailgunError", "verify_webhook_signature", "API_BASE"]
