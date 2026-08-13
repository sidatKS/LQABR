"""Zoom Scheduler client — Server-to-Server OAuth + Scheduler API.

Secrets (Secret Manager): lqabr-zoom-account-id, lqabr-zoom-client-id,
lqabr-zoom-client-secret.

The Scheduling Agent emails leads a Zoom Scheduler booking link; when the
lead books, Zoom's "Schedule created" event webhook notifies us and the
meeting is recorded on the HubSpot contact (MEETING_SCHEDULED).

Typed and mockable — inject a requests.Session in tests.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests

from lqabr_core.secrets import get_secret

OAUTH_URL = "https://zoom.us/oauth/token"
API_BASE = "https://api.zoom.us/v2"


class ZoomError(RuntimeError):
    pass


class ZoomSchedulerClient:
    def __init__(self, account_id: Optional[str] = None, client_id: Optional[str] = None,
                 client_secret: Optional[str] = None,
                 session: Optional[requests.Session] = None) -> None:
        self._account_id = account_id or get_secret("lqabr-zoom-account-id")
        self._client_id = client_id or get_secret("lqabr-zoom-client-id")
        self._client_secret = client_secret or get_secret("lqabr-zoom-client-secret")
        self._session = session or requests.Session()
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0

    # ------------------------------------------------------------------ auth
    def _token(self) -> str:
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token
        resp = self._session.post(
            OAUTH_URL,
            params={"grant_type": "account_credentials", "account_id": self._account_id},
            auth=(self._client_id, self._client_secret),
            timeout=30,
        )
        if resp.status_code != 200:
            raise ZoomError(f"Zoom OAuth failed: HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self._session.get(f"{API_BASE}{path}", params=params or {}, timeout=30,
                                 headers={"Authorization": f"Bearer {self._token()}"})
        if resp.status_code != 200:
            raise ZoomError(f"Zoom GET {path} failed: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ------------------------------------------------------------- scheduler
    def list_schedules(self) -> List[Dict[str, Any]]:
        """The account's Zoom Scheduler schedules (booking pages)."""
        return self._get("/scheduler/schedules").get("schedules", [])

    def booking_link(self, schedule_id: Optional[str] = None) -> str:
        """Booking URL to send to leads.

        Resolution order: ZOOM_BOOKING_URL env override (simplest setup) ->
        the schedule with `schedule_id` -> the account's first schedule.
        """
        override = os.environ.get("ZOOM_BOOKING_URL")
        if override:
            return override
        schedules = self.list_schedules()
        if not schedules:
            raise ZoomError("no Zoom Scheduler schedules found and ZOOM_BOOKING_URL is unset")
        if schedule_id:
            for schedule in schedules:
                if str(schedule.get("schedule_id")) == str(schedule_id):
                    return schedule.get("share_link") or schedule.get("booking_url", "")
            raise ZoomError(f"Zoom schedule {schedule_id} not found")
        first = schedules[0]
        link = first.get("share_link") or first.get("booking_url")
        if not link:
            raise ZoomError("Zoom schedule has no share link")
        return link
