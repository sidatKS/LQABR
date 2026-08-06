"""ADK model wrapper — swap providers via config only, no agent code edits.

Every agent does `model=build_model(MODEL)` instead of `model=MODEL`.
Gemini model names pass straight through to ADK's native handling.
Anything else is wrapped in ADK's LiteLlm adapter (routes the call through
the `litellm` library to the matching provider — Anthropic today, others
later without touching agent code). The provider's API key is bridged from
Secret Manager into the env var the provider's SDK actually expects, since
that name never matches our `lqabr-*` secret naming convention.
"""

from __future__ import annotations

import os
from typing import Any

from lqabr_core.secrets import SecretNotFoundError, get_secret

# provider prefix (in "provider/model" strings) -> (our secret name, the env
# var the provider's own SDK/litellm actually reads it from)
_PROVIDER_SECRETS = {
    "anthropic": ("lqabr-anthropic-api-key", "ANTHROPIC_API_KEY"),
}


def build_model(model_name: str) -> Any:
    """Return whatever ADK's Agent(model=...) should receive for `model_name`."""
    if model_name.startswith("gemini"):
        return model_name

    provider = model_name.split("/", 1)[0]
    if provider in _PROVIDER_SECRETS:
        secret_name, env_name = _PROVIDER_SECRETS[provider]
        if not os.environ.get(env_name):
            try:
                os.environ[env_name] = get_secret(secret_name)
            except SecretNotFoundError:
                pass  # let LiteLlm/the provider SDK raise its own clear error

    try:
        from google.adk.models.lite_llm import LiteLlm
    except ImportError as exc:
        raise ImportError(
            f"model '{model_name}' needs LiteLlm — install with "
            "pip install 'lqabr-core[adk]' (or litellm directly)"
        ) from exc
    return LiteLlm(model=model_name)
