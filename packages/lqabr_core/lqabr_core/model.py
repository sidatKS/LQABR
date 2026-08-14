"""ADK model wrapper — swap providers via config only, no agent code edits.

Every agent does `model=build_model(MODEL)` instead of `model=MODEL`.
Gemini model names pass straight through to ADK's native handling.
Anything else is wrapped in ADK's LiteLlm adapter (routes the call through
the `litellm` library to the matching provider — Anthropic today, others
later without touching agent code). The provider's API key is bridged from
Secret Manager into the env var the provider's SDK actually expects, since
that name never matches our `lqabr-*` secret naming convention.
Shared model-provider wiring — one place to swap Gemini <-> another
provider, so agents never hardcode a provider-specific model object.

A bare Gemini model string (e.g. "gemini-2.0-flash") passes straight
through to ADK's native Gemini client, unchanged from before. Any other
provider string (e.g. "anthropic/claude-sonnet-5") is wrapped in ADK's
LiteLlm so the request routes through litellm instead. Agents keep
importing a plain model-name string from their own env var
(LQABR_<AGENT>_MODEL) — only the wrapping decision lives here.

Requires the `google-adk[extensions]` extra installed when using a
non-Gemini model string (pip install "google-adk[extensions]"; already
listed in requirements.txt for agents that default to it).

Provider API keys are resolved through `lqabr_core.secrets` — i.e. Secret
Manager — and injected into the environment for the SDK to read. See
`ensure_provider_credentials`.
"""

from __future__ import annotations

import os
import logging
import os
from typing import Any, Optional, Tuple

from lqabr_core.secrets import SecretNotFoundError, get_secret

logger = logging.getLogger("lqabr.model")

#: model-name prefix -> (env var the SDK reads, Secret Manager secret name).
#:
#: litellm and google-genai both read their key from the ENVIRONMENT and have
#: no notion of Secret Manager. Without this table the only way to give them a
#: key is a literal in `.env` or `--set-env-vars` — which is precisely the
#: thing we do not want. `ensure_provider_credentials` closes that gap: it
#: resolves the key through `lqabr_core.secrets` (so Secret Manager, subject
#: to LQABR_SECRETS_SOURCE) and puts it where the SDK will find it.
PROVIDER_CREDENTIALS: dict[str, Tuple[str, str]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "lqabr-anthropic-api-key"),
    "gemini": ("GOOGLE_API_KEY", "lqabr-google-api-key"),
    "openai": ("OPENAI_API_KEY", "lqabr-openai-api-key"),
}


def _provider_of(model_name: str) -> str:
    """`anthropic/claude-sonnet-5` -> `anthropic`; `gemini-2.0-flash` -> `gemini`."""
    if "/" in model_name:
        return model_name.split("/", 1)[0].strip().lower()
    return model_name.split("-", 1)[0].strip().lower()


def _using_vertex() -> bool:
    """Vertex authenticates with the runtime service account (ADC), so no API
    key is needed or wanted."""
    return os.environ.get("GOOGLE_GENAI_USE_ENTERPRISE", "").strip().lower() in (
        "1", "true", "yes", "on")


def ensure_provider_credentials(model_name: str) -> Optional[str]:
    """Put the model provider's API key in the environment, sourced through
    Secret Manager, if it is not already set.

    Returns the env var it populated, or None if nothing was needed.

    Deliberately non-fatal: a missing secret is logged, not raised. The
    provider may legitimately be configured another way (Vertex ADC, a key
    already injected by --set-secrets), and failing at import time would take
    the whole container down for a credential that may never be used."""
    provider = _provider_of(model_name)
    entry = PROVIDER_CREDENTIALS.get(provider)
    if entry is None:
        return None

    env_var, secret_name = entry

    if provider == "gemini" and _using_vertex():
        logger.info("model %s uses Vertex AI (ADC) — no API key needed", model_name)
        return None

    if os.environ.get(env_var):
        # Already present. On Cloud Run this is how --set-secrets delivers it,
        # so the value still originates in Secret Manager.
        return None

    try:
        os.environ[env_var] = get_secret(secret_name)
    except SecretNotFoundError as exc:
        logger.warning(
            "model %s needs %s and it is not set; %s could not be resolved (%s). "
            "The model call will fail unless the provider is configured another way.",
            model_name, env_var, secret_name, exc)
        return None

    logger.info("model %s: %s populated from secret %s", model_name, env_var, secret_name)
    return env_var
from typing import Any

from lqabr_core.secrets import SecretNotFoundError, get_secret

# provider prefix (in "provider/model" strings) -> (our secret name, the env
# var the provider's own SDK/litellm actually reads it from)
_PROVIDER_SECRETS = {
    "anthropic": ("lqabr-anthropic-api-key", "ANTHROPIC_API_KEY"),
}


def ensure_provider_key(model_name: str) -> None:
    """Bridge the provider's API key into the env var its SDK/litellm expects.

    Shared by build_model() (the ADK `model=` path) and any code that calls
    litellm directly (e.g. the Text/Voice Agent's Step 7 classification) —
    one place owns the provider -> secret-name mapping so the two paths can't
    drift. Resolution order: the provider's own std env var if already set
    (never overwritten) -> lqabr_core.secrets.get_secret(), which itself
    checks the `LQABR_*`-prefixed env var first, then Secret Manager
    (2026-08-07: routed through Secret Manager on both paths, per user
    request — previously this path was env-only by a 2026-07-31 decision).
    No-op for gemini/unknown providers; swallows SecretNotFoundError so the
    provider SDK raises its own clear error instead.
    """
    provider = model_name.split("/", 1)[0]
    if provider not in _PROVIDER_SECRETS:
        return
    secret_name, env_name = _PROVIDER_SECRETS[provider]
    if os.environ.get(env_name):
        return
    try:
        os.environ[env_name] = get_secret(secret_name)
    except SecretNotFoundError:
        pass  # let LiteLlm/the provider SDK raise its own clear error


def build_model(model_name: str) -> Any:
    """Return whatever ADK's Agent(model=...) should receive for `model_name`."""
    """Return what ADK's `Agent(model=...)` should receive for this
    model name — the bare string for Gemini, or a LiteLlm wrapper for
    everything else."""
    ensure_provider_credentials(model_name)

    if model_name.startswith("gemini"):
        return model_name

    ensure_provider_key(model_name)

    try:
        from google.adk.models.lite_llm import LiteLlm
    except ImportError as exc:
        raise ImportError(
            f"model '{model_name}' needs LiteLlm — install with "
            "pip install 'lqabr-core[adk]' (or litellm directly)"
        ) from exc
    return LiteLlm(model=model_name)
