"""ZoomInfo API client — the automatic ingestion source.

Auth: POST /authenticate with username/password (from Secret Manager:
lqabr-zoominfo-username / lqabr-zoominfo-password) returns a JWT valid ~60
minutes. Search: POST /search/contact with `rs` (result size) — LQABR pulls
batches of 20 by default (`ZOOMINFO_BATCH_SIZE` / --batch-size).

Typed and mockable: pass a requests.Session in tests; no ADK/model imports.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from lqabr_core.secrets import get_secret

BASE_URL = "https://api.zoominfo.com"
DEFAULT_BATCH_SIZE = 20


class ZoomInfoError(RuntimeError):
    pass


class ZoomInfoClient:
    def __init__(self, username: Optional[str] = None, password: Optional[str] = None,
                 session: Optional[requests.Session] = None, max_retries: int = 3) -> None:
        self._username = username or get_secret("lqabr-zoominfo-username")
        self._password = password or get_secret("lqabr-zoominfo-password")
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._jwt: Optional[str] = None
        self._jwt_acquired_at: float = 0.0

    # ------------------------------------------------------------------ auth
    def _token(self) -> str:
        # ZoomInfo JWTs last ~60 min; refresh after 55.
        if self._jwt and (time.time() - self._jwt_acquired_at) < 55 * 60:
            return self._jwt
        resp = self._session.post(f"{BASE_URL}/authenticate",
                                  json={"username": self._username, "password": self._password},
                                  timeout=30)
        if resp.status_code != 200:
            raise ZoomInfoError(f"ZoomInfo auth failed: HTTP {resp.status_code}: {resp.text[:300]}")
        self._jwt = resp.json().get("jwt")
        if not self._jwt:
            raise ZoomInfoError("ZoomInfo auth response had no jwt")
        self._jwt_acquired_at = time.time()
        return self._jwt

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        last: Optional[str] = None
        for attempt in range(self._max_retries):
            resp = self._session.post(f"{BASE_URL}{path}", json=body, timeout=60,
                                      headers={"Authorization": f"Bearer {self._token()}"})
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:   # token expired mid-flight — force refresh
                self._jwt = None
                last = "401 unauthorized"
            elif resp.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {resp.status_code}"
            else:
                raise ZoomInfoError(f"ZoomInfo {path} failed: HTTP {resp.status_code}: {resp.text[:300]}")
            time.sleep(2 ** attempt)
        raise ZoomInfoError(f"ZoomInfo {path} failed after {self._max_retries} retries: {last}")

    # ---------------------------------------------------------------- search
    def search_contacts(self, criteria: Optional[Dict[str, Any]] = None,
                        batch_size: int = DEFAULT_BATCH_SIZE, page: int = 1) -> List[Dict[str, Any]]:
        """One page of contact search results (default LQABR batch: 20)."""
        body: Dict[str, Any] = {"rs": batch_size, "page": page}
        if criteria:
            body.update(criteria)
        data = self._post("/search/contact", body)
        return data.get("data", [])

    # ------------------------------------------------------------- normalize
    @staticmethod
    def normalize(zi_contact: Dict[str, Any]) -> Dict[str, Any]:
        """ZoomInfo contact -> normalized raw lead record (lqabr_core.profile
        key contract). Tolerant of missing fields — the profile builder flags
        non-contactable records downstream."""
        company = zi_contact.get("company") or {}
        name = (zi_contact.get("name")
                or " ".join(x for x in (zi_contact.get("firstName"), zi_contact.get("lastName")) if x))
        location = ", ".join(x for x in (zi_contact.get("city"), zi_contact.get("state"),
                                         zi_contact.get("country")) if x) or None
        revenue = company.get("revenue")
        employees = company.get("employeeCount") or company.get("employees")
        return {
            "full_name": name or None,
            "job_title": zi_contact.get("jobTitle") or zi_contact.get("title"),
            "company": company.get("name") or zi_contact.get("companyName"),
            "email": zi_contact.get("email") or zi_contact.get("emailAddress"),
            "phone": (zi_contact.get("directPhone") or zi_contact.get("mobilePhone")
                      or zi_contact.get("phone")),
            "industry": company.get("primaryIndustry") or company.get("industry"),
            "company_size": str(employees) if employees else None,
            "annual_revenue": str(revenue) if revenue else None,
            "location": location,
            "linkedin_url": zi_contact.get("linkedinUrl") or zi_contact.get("linkedInUrl"),
            "external_employee_id": str(zi_contact.get("id") or "") or None,
            "external_company_id": str(company.get("id") or "") or None,
            "source": "zoominfo",
        }

    def pull_records(self, criteria: Optional[Dict[str, Any]] = None,
                     batch_size: int = DEFAULT_BATCH_SIZE) -> List[Dict[str, Any]]:
        """Search one batch and normalize every contact."""
        return [self.normalize(c) for c in self.search_contacts(criteria, batch_size)]
