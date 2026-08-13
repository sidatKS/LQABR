"""Runtime secret resolution — Google Secret Manager (leadgen path).

Context §7.6 / CLAUDE.md §5: *secrets come from Secret Manager only, never
hard-coded and never committed*. Mahi's wording on 29 Jul was blunter — "we
can't use as an environment variable".

This module is what makes that literally true for the leadgen path. Nothing
reads a credential out of the process environment in production. The values are
fetched over the Secret Manager API using the service account's own identity
(workload identity on Cloud Run — no key file, nothing on disk), held in
memory, and never logged.

NOTE: this is the leadgen-path resolver (logical-name mapping via
``LQABR_SECRET_<LOGICAL>``, backend/TTL, obs audit, fail-closed). It is
deliberately separate from ``lqabr_core.secrets`` (the env-first accessor the
9-pointer agents use); the two coexist while the CRM models are reconciled.

Configuration — all NON-secret, safe to commit:

    LQABR_SECRET_BACKEND      gcp (default) | env
    LQABR_SECRET_PROJECT      GCP project id (falls back to GOOGLE_CLOUD_PROJECT)
    LQABR_SECRET_TTL_SECONDS  in-process cache TTL, default 900
    LQABR_SECRET_<LOGICAL>    the Secret Manager entry for one logical secret.
                              Either a bare id ("lqabr-anthropic-api-key",
                              resolved as .../versions/latest) or a full
                              resource name if you need to pin a version:
                              projects/p/secrets/s/versions/7

Example:

    LQABR_SECRET_ANTHROPIC_API_KEY=lqabr-anthropic-api-key
    LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN=lqabr-hubspot-private-app-token

FAIL CLOSED. The backend defaults to ``gcp`` and there is no silent fallback to
an environment variable — the same reasoning as HUBSPOT_AUTH_MODE (review
finding B11). A misconfigured deploy raises, rather than quietly running on a
stale local value. ``LQABR_SECRET_BACKEND=env`` exists for local development,
CI and tests, and is a decision someone has to type.

Rotation: a rotated secret is picked up on the next run, or after the cache TTL
in a long-lived process. No redeploy, because the version is resolved at
runtime rather than baked into the container.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from lqabr_core.obs import get_obs

DEFAULT_TTL_SECONDS = 900
ENV_BACKEND = "env"
GCP_BACKEND = "gcp"


class SecretConfigError(RuntimeError):
    """The secret is not configured, or the backend is not usable.

    Systemic by nature: no lead is at fault and no lead can succeed.
    """


class SecretAccessError(RuntimeError):
    """Secret Manager refused or failed. Also systemic."""


def _env_key(logical_name: str) -> str:
    return f"LQABR_SECRET_{logical_name.upper()}"


def _redact(value: str) -> str:
    """Never log a secret. Length and a fingerprint are enough to debug with."""
    if not value:
        return "<empty>"
    return f"<{len(value)} chars, ends {value[-4:]}>"


@dataclass
class _CacheEntry:
    value: str
    fetched_at: float
    resource: str


class SecretResolver:
    """Resolves logical secret names to values. One per process."""

    def __init__(
        self,
        backend: str | None = None,
        project: str | None = None,
        ttl_seconds: int | None = None,
        client: Any = None,
    ):
        self._backend = (backend or os.getenv("LQABR_SECRET_BACKEND", GCP_BACKEND)).strip().lower()
        self._project = project or os.getenv("LQABR_SECRET_PROJECT") or os.getenv(
            "GOOGLE_CLOUD_PROJECT"
        )
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else int(os.getenv("LQABR_SECRET_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))
        )
        self._client = client
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    # -- resource naming ----------------------------------------------------

    def _resource_name(self, logical_name: str) -> str:
        configured = (os.getenv(_env_key(logical_name), "") or "").strip()
        # Defensive: python-dotenv can hand us a trailing inline comment as part
        # of the value ("  # default: ..."), and a secret id can never contain
        # '#'. Without this, the resolver asks GCP for a secret literally named
        # "# default: lqabr-anthropic-api-key" (seen in the wild, 31 Jul).
        configured = configured.split("#", 1)[0].strip()
        if not configured:
            # Fall back to a predictable id so a fresh deploy only has to set the
            # project, not five separate names.
            configured = "lqabr-" + logical_name.lower().replace("_", "-")
        if configured.startswith("projects/"):
            return configured
        if not self._project:
            raise SecretConfigError(
                f"cannot resolve secret {logical_name!r}: no GCP project. Set "
                "LQABR_SECRET_PROJECT (or GOOGLE_CLOUD_PROJECT), or set "
                f"{_env_key(logical_name)} to a full "
                "projects/<p>/secrets/<s>/versions/<v> resource name."
            )
        return f"projects/{self._project}/secrets/{configured}/versions/latest"

    # -- backends -----------------------------------------------------------

    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google.cloud import secretmanager
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise SecretConfigError(
                "google-cloud-secret-manager is not installed but "
                "LQABR_SECRET_BACKEND=gcp. Run `uv sync`, or set "
                "LQABR_SECRET_BACKEND=env for local development."
            ) from exc
        self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def _fetch_gcp(self, logical_name: str) -> tuple[str, str]:
        resource = self._resource_name(logical_name)
        obs = get_obs()
        # audit: every external call. The RESOURCE is logged, never the value.
        with obs.timed_audit(
            "secret_manager_access", endpoint=resource, method="AccessSecretVersion"
        ) as timer:
            try:
                response = self._client_or_raise().access_secret_version(name=resource)
            except Exception as exc:
                timer.extra["outcome"] = "error"
                raise SecretAccessError(
                    f"could not read {resource}: {type(exc).__name__}: {exc}. "
                    "Check that the runtime service account has "
                    "roles/secretmanager.secretAccessor on this secret."
                ) from exc
            timer.extra["status"] = 200
        value = response.payload.data.decode("utf-8").strip()
        if not value:
            raise SecretConfigError(f"{resource} resolved to an empty value")
        return value, resource

    def _fetch_env(self, logical_name: str) -> tuple[str, str]:
        value = (os.getenv(logical_name.upper(), "") or "").strip()
        if not value:
            raise SecretConfigError(
                f"LQABR_SECRET_BACKEND=env but {logical_name.upper()} is not set. "
                "This backend is for local development, CI and tests only."
            )
        return value, f"env:{logical_name.upper()}"

    # -- public -------------------------------------------------------------

    def get(self, logical_name: str, *, required: bool = True) -> str | None:
        """Resolve one secret. Cached in-process for the TTL."""
        try:
            with self._lock:
                cached = self._cache.get(logical_name)
                if cached and (time.time() - cached.fetched_at) < self._ttl:
                    return cached.value

                if self._backend == ENV_BACKEND:
                    value, resource = self._fetch_env(logical_name)
                elif self._backend == GCP_BACKEND:
                    value, resource = self._fetch_gcp(logical_name)
                else:
                    raise SecretConfigError(
                        f"unknown LQABR_SECRET_BACKEND: {self._backend!r} "
                        f"(expected {GCP_BACKEND!r} or {ENV_BACKEND!r})"
                    )

                self._cache[logical_name] = _CacheEntry(value, time.time(), resource)

            get_obs().process.emit(
                "secret_resolved",
                secret=logical_name,
                backend=self._backend,
                resource=resource,
                value=_redact(value),  # never the value itself
            )
            return value
        except (SecretConfigError, SecretAccessError):
            if required:
                raise
            return None

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


_RESOLVER: SecretResolver | None = None
_RESOLVER_LOCK = threading.Lock()


def get_resolver() -> SecretResolver:
    global _RESOLVER
    with _RESOLVER_LOCK:
        if _RESOLVER is None:
            _RESOLVER = SecretResolver()
        return _RESOLVER


def get_secret(logical_name: str, *, required: bool = True) -> str | None:
    """Resolve a secret by its logical name. The one entry point every agent uses."""
    return get_resolver().get(logical_name, required=required)


def reset_resolver(resolver: SecretResolver | None = None) -> SecretResolver:
    """Reset the process resolver. Tests, and credential rotation."""
    global _RESOLVER
    with _RESOLVER_LOCK:
        _RESOLVER = resolver or SecretResolver()
        return _RESOLVER
