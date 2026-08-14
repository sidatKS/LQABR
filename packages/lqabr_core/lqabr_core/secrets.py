"""Secret access for LQABR.

All service credentials (MailGun, Twilio, HubSpot, ZoomInfo, Zoom, the model
provider) live in Google Secret Manager — never hard-coded, never committed.
Locally, a git-ignored `.env` exporting the same names is the fallback so
agents run without GCP access.

Secret names (provisioned by infra/gcp/02_secret_manager.sh via config.sh /
config.dev.sh):

    lqabr-hubspot-access-token
    lqabr-mailgun-api-key
    lqabr-mailgun-webhook-signing-key
    lqabr-twilio-account-sid
    lqabr-twilio-auth-token
    lqabr-zoominfo-username
    lqabr-zoominfo-password
    lqabr-zoom-account-id
    lqabr-zoom-client-id
    lqabr-zoom-client-secret
    lqabr-zoom-webhook-secret-token
    lqabr-anthropic-api-key   (bridged to ANTHROPIC_API_KEY for litellm/ADK
                                by lqabr_core.model.ensure_provider_key())

Where a value comes from is controlled by ``LQABR_SECRETS_SOURCE``:

    auto            (default) environment variable first, then the Secret
                    Manager API. This is the Cloud Run shape: `--set-secrets`
                    reads Secret Manager at revision-deploy time and injects
                    the value AS an environment variable, so "env first" IS
                    Secret Manager in a properly deployed service — no API
                    call on the hot path, nothing extra on a cold start.
    secret_manager  Ignore the environment entirely and always call the API.
                    Use this to PROVE a value is coming from Secret Manager,
                    or when rotation must take effect without a redeploy.
                    Costs one API call per secret per process.
    env             Never call the API. Local development and tests.

Whichever source answers, `get_secret` logs the secret NAME and the source it
came from at INFO — never the value. Before this existed there was no way to
tell whether a running service was using a Secret Manager value or a literal
someone had left in a `.env`, which is exactly the question that prompted it.

*** The API path needs `google-cloud-secret-manager` installed. *** It is an
optional extra (`pip install 'lqabr-core[gcp]'`), and the Cloud Run image
installs it — see infra/gcp/cloud-run/Dockerfile. Without it the API path
cannot run at all and an environment variable is the only thing that can
resolve a secret.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional


logger = logging.getLogger("lqabr.secrets")


class SecretNotFoundError(RuntimeError):
    pass


def _env_name(secret_name: str) -> str:
    return secret_name.upper().replace("-", "_")


VALID_SOURCES = ("auto", "secret_manager", "env")


def secrets_source() -> str:
    """Which source `get_secret` is allowed to use. See the module docstring."""
    source = os.environ.get("LQABR_SECRETS_SOURCE", "auto").strip().lower()
    if source not in VALID_SOURCES:
        raise SecretNotFoundError(
            f"LQABR_SECRETS_SOURCE={source!r} is not one of {VALID_SOURCES}")
    return source


@lru_cache(maxsize=64)
def get_secret(secret_name: str, project_id: Optional[str] = None) -> str:
    """Fetch a secret by its Secret Manager name.

    Raises SecretNotFoundError rather than returning an empty string: a
    silently-empty credential fails later, somewhere less obvious."""
    source = secrets_source()

    # --- environment ---------------------------------------------------
    if source in ("auto", "env"):
        env_value = os.environ.get(_env_name(secret_name))
        if env_value:
            logger.info("secret %s resolved from environment (%s)",
                        secret_name, _env_name(secret_name))
            return env_value
        if source == "env":
            raise SecretNotFoundError(
                f"secret '{secret_name}' not in env ({_env_name(secret_name)}) and "
                "LQABR_SECRETS_SOURCE=env forbids the Secret Manager API"
            )

    # --- Secret Manager API --------------------------------------------
    project = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SecretNotFoundError(
            f"secret '{secret_name}' could not be resolved: GOOGLE_CLOUD_PROJECT "
            f"is unset, so Secret Manager cannot be queried"
            + ("" if source == "secret_manager"
               else f" and {_env_name(secret_name)} is not set either")
        )

    try:
        from google.cloud import secretmanager  # extra: lqabr-core[gcp]
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise SecretNotFoundError(
            f"secret '{secret_name}' needs the Secret Manager client, which is not "
            "installed. Install the extra (pip install 'lqabr-core[gcp]'); the "
            "Cloud Run image already does. Without it an environment variable is "
            "the ONLY thing that can resolve a secret."
        ) from exc

    name = f"projects/{project}/secrets/{secret_name}/versions/latest"
    try:
        # The CLIENT CONSTRUCTOR resolves Application Default Credentials and
        # raises DefaultCredentialsError when there are none — so it has to be
        # inside the guard too. Left outside, that error escaped as an
        # unhandled 500 from any caller that did not wrap `get_secret` itself,
        # e.g. Mailgun signature verification.
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(request={"name": name})
    except Exception as exc:  # noqa: BLE001 - normalised to SecretNotFoundError
        raise SecretNotFoundError(
            f"Secret Manager lookup failed for '{secret_name}' in project "
            f"'{project}': {exc}. Check the secret exists and the runtime service "
            "account has roles/secretmanager.secretAccessor."
        ) from exc

    logger.info("secret %s resolved from Secret Manager (projects/%s)",
                secret_name, project)
    return response.payload.data.decode("utf-8")
