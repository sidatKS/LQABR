"""Secret resolution — Secret Manager first, environment as the fallback.

One rule, enforced here rather than trusted to callers: the NAME of a secret
and WHERE it came from are logged; the value never is. A missing secret raises
with the name and both places it was looked for, so the fix is obvious from the
error alone.

`secrets_source` decides the order:

    secret_manager  Secret Manager only — a missing secret is an error
    env             the environment only — no GCP call is made
    auto            environment first, then Secret Manager (local-dev friendly)
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from .settings import Settings, get_settings
except ImportError:  # pragma: no cover - direct import in tests
    from settings import Settings, get_settings  # type: ignore


class SecretError(RuntimeError):
    """A required secret could not be resolved. Names the secret, never a value."""


def _env_name(secret_name: str) -> str:
    """`lqabr-hubspot-access-token` -> `LQABR_HUBSPOT_ACCESS_TOKEN`."""
    return secret_name.upper().replace("-", "_")


def _from_secret_manager(secret_name: str, project: str) -> Optional[str]:
    if not project:
        raise SecretError(
            f"cannot read secret {secret_name!r}: no GCP project is configured. "
            "Set LQABR_RESEARCH_GCP_PROJECT (or GOOGLE_CLOUD_PROJECT), or use "
            "LQABR_RESEARCH_SECRETS_SOURCE=env with the value in the environment.")
    try:
        from google.cloud import secretmanager
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SecretError(
            f"cannot read secret {secret_name!r}: google-cloud-secret-manager is "
            "not installed — add it, or use LQABR_RESEARCH_SECRETS_SOURCE=env"
        ) from exc

    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{project}/secrets/{secret_name}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": path})
    except Exception as exc:  # noqa: BLE001 - re-raised named, never swallowed
        raise SecretError(
            f"cannot read secret {secret_name!r} from {path}: {exc}. Check the "
            "secret exists and the runtime account has "
            "roles/secretmanager.secretAccessor.") from exc
    return response.payload.data.decode("utf-8").strip()


def resolve_secret(secret_name: str, *, settings: Optional[Settings] = None,
                   obs=None) -> str:
    """The secret's value, or SecretError naming where it was looked for."""
    settings = settings or get_settings()
    source = (settings.secrets_source or "auto").lower()
    env_name = _env_name(secret_name)
    project = settings.gcp_project or os.environ.get("GOOGLE_CLOUD_PROJECT", "")

    def _emit(where: str) -> None:
        if obs is not None:
            obs.process.emit("secret_resolved", secret=secret_name, source=where)

    if source in ("env", "auto"):
        value = os.environ.get(env_name, "").strip()
        if value:
            _emit(f"env:{env_name}")
            return value
        if source == "env":
            raise SecretError(
                f"secret {secret_name!r} is not set: LQABR_RESEARCH_SECRETS_SOURCE=env "
                f"so only the environment was checked, and {env_name} is empty.")

    value = _from_secret_manager(secret_name, project)
    if not value:
        raise SecretError(f"secret {secret_name!r} resolved to an empty value in "
                          f"project {project!r}")
    _emit(f"secret_manager:{project}/{secret_name}")
    return value
