"""The orchestrating model, with its API key resolved from Secret Manager.

Why a subclass rather than passing ``api_key=`` at construction: ``root_agent``
is a module-level object, so anything resolved in its constructor runs at
IMPORT time — on every `adk web` startup, every `adk run`, and every test
collection. That would mean a Secret Manager round-trip (and a credential
requirement) just to import the module.

So the key is resolved lazily, on the first model call, and cached by
lqabr_core.leadgen.secrets for the TTL. Import stays free and offline.

The key is passed straight into the LiteLLM call arguments. It is never written
to ``os.environ`` — putting it there is what context §7.6 and Mahi's "we can't
use as an environment variable" were about, and it would leak into any child
process and into anything that dumps the environment on error.
"""

from __future__ import annotations

import os
from typing import Any, AsyncGenerator

from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_response import LlmResponse

from lqabr_core.obs import get_obs

# Which secret holds the key, per provider prefix. Extend as providers are added.
PROVIDER_SECRET = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "vertex_ai": "GOOGLE_API_KEY",
}


class SecretBackedLiteLlm(LiteLlm):
    """LiteLlm that fetches its API key from Secret Manager on first use."""

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._ensure_api_key()
        async for response in super().generate_content_async(llm_request, stream=stream):
            yield response

    # -- internals ----------------------------------------------------------

    def _ensure_api_key(self) -> None:
        args = getattr(self, "_additional_args", None)
        if args is None or args.get("api_key"):
            return

        provider = str(self.model).split("/", 1)[0].lower()
        secret_name = PROVIDER_SECRET.get(provider)
        if secret_name is None:
            # An unknown provider: let LiteLLM resolve credentials its own way
            # rather than guess at a secret name.
            get_obs().process.emit(
                "model_key_not_managed",
                provider=provider,
                note="no Secret Manager mapping for this provider; leaving to LiteLLM",
            )
            return

        # Go through the resolver, and ONLY the resolver. An earlier version
        # checked os.environ first "for local dev" — which meant that in
        # production, with the gcp backend, a stray ANTHROPIC_API_KEY in the
        # environment silently beat Secret Manager. That is the exact failure
        # mode lqabr_core.leadgen.secrets fails closed to prevent, so the
        # shortcut is gone. Local dev gets the same escape hatch as every other
        # credential: LQABR_SECRET_BACKEND=env.
        from lqabr_core.leadgen.secrets import get_secret

        args["api_key"] = get_secret(secret_name)
        get_obs().process.emit(
            "model_key_resolved",
            provider=provider,
            secret=secret_name,
            note="held in memory for this process only; never written to os.environ",
        )
