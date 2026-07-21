"""Mailgun client — outbound email + webhook signature verification.

Secrets (Secret Manager): lqabr-mailgun-api-key, lqabr-mailgun-webhook-signing-key.
Config (env): MAILGUN_DOMAIN, MAILGUN_FROM (e.g. "LQABR <outreach@mg.example.com>").

Tracking: messages are sent with opens/clicks tracking enabled; Mailgun then
POSTs `delivered` / `opened` / `clicked` events to our webhook
(webhook_app.py), which records them on the HubSpot contact.

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
                 max_retries: int = 3) -> None:
        self._api_key = api_key or get_secret("lqabr-mailgun-api-key")
        self._domain = domain or os.environ.get("MAILGUN_DOMAIN", "")
        if not self._domain:
            raise MailgunError("MAILGUN_DOMAIN is not configured")
        self._sender = sender or os.environ.get("MAILGUN_FROM", f"LQABR <outreach@{self._domain}>")
        self._session = session or requests.Session()
        self._max_retries = max_retries

    def send_email(self, to: str, subject: str, html: str, text: Optional[str] = None,
                   tags: Optional[list[str]] = None,
                   variables: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Send one tracked email. `variables` are attached as Mailgun user
        variables and echoed back in webhook events (we always attach
        hubspot_contact_id so events can be tied back to the lead)."""
        data: Dict[str, Any] = {
            "from": self._sender,
            "to": to,
            "subject": subject,
            "html": html,
            "o:tracking": "yes",
            "o:tracking-opens": "yes",
            "o:tracking-clicks": "yes",
        }
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
            except requests.RequestException as exc:
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
    """Verify a Mailgun webhook payload (HMAC-SHA256 of timestamp+token)."""
    key = signing_key or get_secret("lqabr-mailgun-webhook-signing-key")
    expected = hmac.new(key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
