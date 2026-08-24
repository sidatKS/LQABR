"""Secret resolution — this agent's own copy, deliberately.

Three backends, chosen by LQABR_SUMMARY_SECRETS_SOURCE:

    env             read the environment. Local dev; also the shape Cloud
                    Run's --set-secrets injection produces.
    secret_manager  always call the Secret Manager API. Makes "it comes from
                    Secret Manager" provable rather than assumed.
    auto            prefer the environment, fall back to the API.

Every resolution logs the secret's NAME and WHERE it came from. The value is
never logged, never returned in an error message, and never put in a trace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .settings import Settings, get_settings


class SecretError(RuntimeError):
    """A required secret could not be resolved. Names the secret, not its value."""


@dataclass(frozen=True)
class ResolvedSecret:
    name: str
    source: str          # env | secret_manager | absent
    value: str = ""

    @property
    def found(self) -> bool:
        return bool(self.value)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"ResolvedSecret(name={self.name!r}, source={self.source!r}, value=<redacted>)"


def _from_secret_manager(secret_name: str, project: str) -> Optional[str]:
    if not project:
        raise SecretError(
            f"{secret_name}: LQABR_SUMMARY_GCP_PROJECT is unset, so the Secret "
            "Manager backend has no project to read from"
        )
    try:
        from google.cloud import secretmanager
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SecretError(
            f"{secret_name}: google-cloud-secret-manager is not installed; "
            "use LQABR_SUMMARY_SECRETS_SOURCE=env for local runs"
        ) from exc
    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{project}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": path})
    return response.payload.data.decode("utf-8")


def resolve(
    secret_name: str,
    *,
    env_var: str = "",
    required: bool = False,
    settings: Settings | None = None,
) -> ResolvedSecret:
    """Resolve one secret. `secret_name` is the Secret Manager name; `env_var`
    is where the same value lives in the environment."""
    settings = settings or get_settings()
    env_var = env_var or secret_name.upper().replace("-", "_")
    source = settings.secrets_source

    value = ""
    origin = "absent"

    if source in ("env", "auto"):
        value = (os.environ.get(env_var) or "").strip()
        origin = "env" if value else "absent"

    if not value and source in ("secret_manager", "auto"):
        fetched = _from_secret_manager(secret_name, settings.gcp_project)
        if fetched:
            value = fetched.strip()
            origin = "secret_manager"

    if required and not value:
        raise SecretError(
            f"required secret {secret_name!r} not found "
            f"(backend={source}, env_var={env_var})"
        )
    return ResolvedSecret(name=secret_name, source=origin, value=value)


#: The provider key each LiteLLM prefix needs, and the Secret Manager name
#: it lives under in this project. Extend here, never at the call site.
PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY", "lqabr-anthropic-api-key"),
    "claude": ("ANTHROPIC_API_KEY", "lqabr-anthropic-api-key"),
    "gemini": ("GOOGLE_API_KEY", "lqabr-google-api-key"),
    "vertex_ai": ("GOOGLE_API_KEY", "lqabr-google-api-key"),
    "openai": ("OPENAI_API_KEY", "lqabr-openai-api-key"),
}


def ensure_provider_credentials(model: str, *, settings: Settings | None = None) -> ResolvedSecret:
    """Put the model provider's key in the environment, where LiteLLM looks.

    Returns the resolution so the caller can log the NAME and SOURCE. A model
    id with no known prefix is left alone — an unknown provider is not an
    error here, it is simply not ours to configure.
    """
    settings = settings or get_settings()
    prefix = model.split("/", 1)[0].strip().lower() if "/" in model else model.strip().lower()
    entry = PROVIDER_KEYS.get(prefix)
    if not entry:
        return ResolvedSecret(name=f"<unknown provider: {prefix}>", source="absent")
    env_var, secret_name = entry
    resolved = resolve(secret_name, env_var=env_var, settings=settings)
    if resolved.found:
        os.environ.setdefault(env_var, resolved.value)
    return resolved
