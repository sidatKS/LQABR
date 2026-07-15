"""Secret access for LQABR.

All service credentials (MailGun, Twilio, HubSpot, ZoomInfo, Zoom) live in
Google Secret Manager — never hard-coded, never committed. Locally, a
git-ignored `.env` exporting the same names is the fallback so agents run
without GCP access.

Secret names (provisioned by infra/gcp/02_secret_manager.sh):

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

Resolution order:
    1. Environment variable (secret name upper-cased, dashes -> underscores,
       e.g. LQABR_HUBSPOT_ACCESS_TOKEN). This is also how Cloud Run's
       `--set-secrets` injection surfaces Secret Manager values.
    2. Secret Manager API (needs GOOGLE_CLOUD_PROJECT + ADC credentials).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional


class SecretNotFoundError(RuntimeError):
    pass


def _env_name(secret_name: str) -> str:
    return secret_name.upper().replace("-", "_")


@lru_cache(maxsize=64)
def get_secret(secret_name: str, project_id: Optional[str] = None) -> str:
    """Fetch a secret by its Secret Manager name, env var first."""
    env_value = os.environ.get(_env_name(secret_name))
    if env_value:
        return env_value

    project = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SecretNotFoundError(
            f"secret '{secret_name}' not in env ({_env_name(secret_name)}) and "
            "GOOGLE_CLOUD_PROJECT is unset — cannot query Secret Manager"
        )

    try:
        from google.cloud import secretmanager  # optional dep: lqabr-core[gcp]
    except ImportError as exc:  # pragma: no cover
        raise SecretNotFoundError(
            f"secret '{secret_name}' not in env and google-cloud-secret-manager "
            "is not installed (pip install 'lqabr-core[gcp]')"
        ) from exc

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_name}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
    except Exception as exc:  # noqa: BLE001
        raise SecretNotFoundError(f"Secret Manager lookup failed for '{secret_name}': {exc}") from exc
    return re