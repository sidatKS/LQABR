"""Twilio client — SMS + programmable voice, via the Twilio REST API.

Secrets (Secret Manager): lqabr-twilio-account-sid, lqabr-twilio-auth-token.
Config (env): TWILIO_FROM_NUMBER (E.164), LQABR_WEBHOOK_BASE_URL (public base
URL of the text_voice webhook service, e.g. the Cloud Run URL).

Outbound calls are created with answering-machine detection
(MachineDetection=DetectMessageEnd) so the webhook can branch:
    human answers  -> conversational Q&A flow (conversation.py)
    voicemail      -> deliver the customized voicemail, then send the
                      customized SMS follow-up

Typed and mockable — inject a requests.Session in tests. No Twilio SDK
dependency; the REST surface we need is small and explicit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any, Dict, Optional

import requests

from lqabr_core.secrets import get_secret

API_BASE = "https://api.twilio.com/2010-04-01"


class TwilioError(RuntimeError):
    pass


class TwilioClient:
    def __init__(self, account_sid: Optional[str] = None, auth_token: Optional[str] = None,
                 from_number: Optional[str] = None, session: Optional[requests.Session] = None,
                 max_retries: int = 3) -> None:
        self._sid = account_sid or get_secret("lqabr-twilio-account-sid")
        self._token = auth_token or get_secret("lqabr-twilio-auth-token")
        self._from = from_number or os.environ.get("TWILIO_FROM_NUMBER", "")
        if not self._from:
            raise TwilioError("TWILIO_FROM_NUMBER is not configured")
        self._session = session or requests.Session()
        self._max_retries = max_retries

    def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{API_BASE}/Accounts/{self._sid}{path}"
        last: Optional[str] = None
        for attempt in range(self._max_retries):
            try:
                resp = self._session.post(url, data=data, auth=(self._sid, self._token), timeout=30)
            except requests.RequestException as exc:
                last = str(exc)
            else:
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {resp.status_code}"
                else:
                    raise TwilioError(f"Twilio {path} failed: HTTP {resp.status_code}: {resp.text[:300]}")
            time.sleep(2 ** attempt)
        raise TwilioError(f"Twilio {path} failed after {self._max_retries} retries: {last}")

    # ------------------------------------------------------------------ sms
    def send_sms(self, to: str, body: str, status_callback: Optional[str] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {"From": self._from, "To": to, "Body": body}
        if status_callback:
            data["StatusCallback"] = status_callback
        return self._post("/Messages.json", data)

    # ---------------------------------------------------------------- voice
    def place_call(self, to: str, answer_url: str, status_callback: Optional[str] = None) -> Dict[str, Any]:
        """Start an outbound call with answering-machine detection. Twilio
        requests TwiML from `answer_url` when the call connects; AnsweredBy
        tells the webhook whether a human or machine picked up."""
        data: Dict[str, Any] = {
            "From": self._from,
            "To": to,
            "Url": answer_url,
            "MachineDetection": "DetectMessageEnd",
            "MachineDetectionTimeout": "30",
        }
        if status_callback:
            data["StatusCallback"] = status_callback
            data["StatusCallbackEvent"] = "completed"
        return self._post("/Calls.json", data)


def validate_twilio_signature(url: str, params: Dict[str, str], signature: str,
                              auth_token: Optional[str] = None) -> bool:
    """Validate X-Twilio-Signature (HMAC-SHA1 over URL + sorted form params)."""
    token = auth_token or get_secret("lqabr-twilio-auth-token")
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)
