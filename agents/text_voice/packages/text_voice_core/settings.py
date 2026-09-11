"""The only module in this agent that reads the environment.

Everything outside the agent — the model, the MCP URL, the Vapi ids, the
qualifying email statuses — is a name with a default here, so a rename out
there is a config change and never a code edit.

Precedence today is: environment variable -> code default. The scaffold's
middle layer (config/config.yaml) is NOT wired yet; when it lands it slots
into _get() and nothing else has to change.

Read once at import. The suite's conftest strips LQABR_* before collection,
so a developer's .env cannot leak into a test run.
"""

from __future__ import annotations

import os
from typing import FrozenSet, Optional

# --- Step 7: the model that classifies a finished call ---------------------
MODEL: str = os.environ.get("LQABR_TEXT_VOICE_MODEL", "anthropic/claude-sonnet-5")

# The Secret Manager name the Anthropic key is resolved from at call time.
ANTHROPIC_SECRET_NAME: str = "lqabr-anthropic-api-key"

# --- Step 5: the central MCP server ----------------------------------------
MCP_BASE_URL: str = os.environ.get("LQABR_MCP_BASE_URL", "http://localhost:8080/mcp")
MCP_TIMEOUT_SECONDS: int = int(os.environ.get("LQABR_MCP_TIMEOUT_SECONDS", "30"))

# The MCP argument name carrying the record id. A wire spelling, never a
# literal at the call site: it changed under this agent once already, and a
# rename on the server's side must cost a config flip, not an edit.
MCP_OBJECT_ID_ARG: str = os.environ.get("LQABR_MCP_OBJECT_ID_ARG", "object_id")

# --- Step 4: Vapi, the one outbound telephony leg --------------------------
VAPI_BASE_URL: str = os.environ.get("LQABR_VAPI_BASE_URL", "https://api.vapi.ai")
VAPI_PHONE_NUMBER_ID: str = os.environ.get("LQABR_VAPI_PHONE_NUMBER_ID", "")
VAPI_ASSISTANT_ID: str = os.environ.get("LQABR_VAPI_ASSISTANT_ID", "")

# Env-only by design — this credential is deliberately not read from Secret
# Manager. The Secret Manager NAME is kept so logs can cite it without ever
# touching the value.
VAPI_CREDENTIAL_NAME: str = "lqabr-vapi-api-key"
VAPI_API_KEY_ENV: str = VAPI_CREDENTIAL_NAME.upper().replace("-", "_")


def vapi_api_key() -> str:
    """Read at call time, not import time, so a rotated key needs no restart
    and the suite can set it per-test."""
    return os.environ.get(VAPI_API_KEY_ENV, "")


# --- Step 3: who is eligible for a call ------------------------------------
# Rev 5 says the workflow fires on "clicked", but the real lqabr_email_status
# enumeration in portal 246777241 has no CLICKED option, so OPENED is in use.
QUALIFIED_EMAIL_STATUSES: FrozenSet[str] = frozenset(
    s.strip().upper() for s in
    os.environ.get("LQABR_QUALIFIED_EMAIL_STATUSES", "OPENED").split(",")
    if s.strip())

# --- Outreach identity and lead_context ------------------------------------
# Unset is accepted and correct: the assistant introduces itself as
# "the LQABR team" (confirmed 2026-08-18).
SENDER_NAME: str = os.environ.get("LQABR_SENDER_NAME", "the LQABR team")

LEAD_CONTEXT_MAX_CHARS: int = int(
    os.environ.get("LQABR_LEAD_CONTEXT_MAX_CHARS", "1000"))


# --- Observability ------------------------------------------------------
# Read by observability.configure(). JSON lines on stdout is what Cloud Run
# ingests; "0" gives readable local logs.
LOG_LEVEL: str = os.environ.get("LQABR_LOG_LEVEL", "INFO")
LOG_JSON: bool = os.environ.get("LQABR_LOG_JSON", "1") != "0"

# --- Cloud Run runtime facts, stamped on the startup/shutdown records ------
# Set by the platform, not by us; absent locally, which is correct.
K_REVISION: Optional[str] = os.environ.get("K_REVISION")
GCP_PROJECT: Optional[str] = os.environ.get("GOOGLE_CLOUD_PROJECT")


def export_provider_key(name: str, value: str) -> None:
    """Publish a provider credential into the environment for a library that
    reads it there (litellm reads ANTHROPIC_API_KEY itself).

    Here rather than at the call site so settings.py stays the single module
    touching os.environ — and so there is one place to audit what this agent
    writes into its own environment. The value is never logged.
    """
    os.environ[name] = value


def describe() -> dict:
    """Config as it resolved, for the startup log. Names and flags only —
    never a credential value."""
    return {
        "model": MODEL,
        "mcp_base_url": MCP_BASE_URL,
        "mcp_timeout_seconds": MCP_TIMEOUT_SECONDS,
        "mcp_object_id_arg": MCP_OBJECT_ID_ARG,
        "vapi_base_url": VAPI_BASE_URL,
        "vapi_phone_number_id_set": bool(VAPI_PHONE_NUMBER_ID),
        "vapi_assistant_id_set": bool(VAPI_ASSISTANT_ID),
        "vapi_api_key_set": bool(vapi_api_key()),
        "qualified_email_statuses": sorted(QUALIFIED_EMAIL_STATUSES),
        "sender_name_set": bool(os.environ.get("LQABR_SENDER_NAME")),
        "lead_context_max_chars": LEAD_CONTEXT_MAX_CHARS,
    }
