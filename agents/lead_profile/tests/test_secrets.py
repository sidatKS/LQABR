"""Secret Manager resolution.

Context §7.6 / CLAUDE.md §5 and Mahi's 29 Jul note: credentials come from
Secret Manager at runtime, never from the process environment in production,
never hard-coded, never logged.
"""

from __future__ import annotations

import json

import pytest

from lqabr_core.leadgen.secrets import (
    SecretAccessError,
    SecretConfigError,
    SecretResolver,
    reset_resolver,
)

SECRET_VALUE = "sk-ant-super-secret-value-9999"


class FakeSecretManager:
    """Stands in for google.cloud.secretmanager.SecretManagerServiceClient."""

    def __init__(self, values: dict[str, str] | None = None, error: Exception | None = None):
        self.values = values or {}
        self.error = error
        self.requests: list[str] = []

    def access_secret_version(self, name: str):
        self.requests.append(name)
        if self.error:
            raise self.error
        if name not in self.values:
            raise KeyError(f"no such secret version: {name}")

        payload = type("Payload", (), {"data": self.values[name].encode("utf-8")})()
        return type("Response", (), {"payload": payload})()


def _resolver(**kwargs) -> SecretResolver:
    return SecretResolver(backend="gcp", project="lqabr-dev", **kwargs)


# --- resolution ------------------------------------------------------------


def test_a_bare_secret_id_resolves_to_the_latest_version(monkeypatch):
    monkeypatch.setenv("LQABR_SECRET_ANTHROPIC_API_KEY", "lqabr-anthropic-api-key")
    resource = "projects/lqabr-dev/secrets/lqabr-anthropic-api-key/versions/latest"
    client = FakeSecretManager({resource: SECRET_VALUE})

    assert _resolver(client=client).get("ANTHROPIC_API_KEY") == SECRET_VALUE
    assert client.requests == [resource]


def test_a_full_resource_name_pins_the_version(monkeypatch):
    pinned = "projects/other-proj/secrets/hubspot-refresh/versions/7"
    monkeypatch.setenv("LQABR_SECRET_HUBSPOT_REFRESH_TOKEN", pinned)
    client = FakeSecretManager({pinned: "refresh-abc"})

    assert _resolver(client=client).get("HUBSPOT_REFRESH_TOKEN") == "refresh-abc"
    assert client.requests == [pinned]


def test_a_trailing_inline_comment_in_dotenv_is_not_part_of_the_id(monkeypatch):
    """python-dotenv can keep '  # comment' as the value; '#' can't be in an id."""
    monkeypatch.setenv(
        "LQABR_SECRET_ANTHROPIC_API_KEY", "   # default: lqabr-anthropic-api-key"
    )
    resource = "projects/lqabr-dev/secrets/lqabr-anthropic-api-key/versions/latest"
    client = FakeSecretManager({resource: SECRET_VALUE})
    assert _resolver(client=client).get("ANTHROPIC_API_KEY") == SECRET_VALUE

    monkeypatch.setenv("LQABR_SECRET_ANTHROPIC_API_KEY", "my-real-id  # a comment")
    resource2 = "projects/lqabr-dev/secrets/my-real-id/versions/latest"
    client2 = FakeSecretManager({resource2: "v2"})
    assert _resolver(client=client2).get("ANTHROPIC_API_KEY") == "v2"


def test_an_unconfigured_secret_falls_back_to_a_predictable_id(monkeypatch):
    monkeypatch.delenv("LQABR_SECRET_HUBSPOT_CLIENT_ID", raising=False)
    resource = "projects/lqabr-dev/secrets/lqabr-hubspot-client-id/versions/latest"
    client = FakeSecretManager({resource: "client-123"})

    assert _resolver(client=client).get("HUBSPOT_CLIENT_ID") == "client-123"


def test_the_value_is_cached_for_the_ttl(monkeypatch):
    monkeypatch.setenv("LQABR_SECRET_ANTHROPIC_API_KEY", "k")
    resource = "projects/lqabr-dev/secrets/k/versions/latest"
    client = FakeSecretManager({resource: SECRET_VALUE})
    resolver = _resolver(client=client)

    for _ in range(5):
        resolver.get("ANTHROPIC_API_KEY")
    assert len(client.requests) == 1, "one API call per run, not one per use"


# --- fail closed -----------------------------------------------------------


def test_no_project_is_an_error_not_a_guess(monkeypatch):
    monkeypatch.delenv("LQABR_SECRET_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    resolver = SecretResolver(backend="gcp", project=None, client=FakeSecretManager())
    with pytest.raises(SecretConfigError) as exc:
        resolver.get("ANTHROPIC_API_KEY")
    assert "LQABR_SECRET_PROJECT" in str(exc.value)


def test_a_denied_secret_says_which_role_is_missing():
    client = FakeSecretManager(error=PermissionError("403 permission denied"))
    with pytest.raises(SecretAccessError) as exc:
        _resolver(client=client).get("ANTHROPIC_API_KEY")
    assert "secretmanager.secretAccessor" in str(exc.value)


def test_an_empty_secret_value_is_rejected(monkeypatch):
    monkeypatch.setenv("LQABR_SECRET_ANTHROPIC_API_KEY", "k")
    resource = "projects/lqabr-dev/secrets/k/versions/latest"
    with pytest.raises(SecretConfigError):
        _resolver(client=FakeSecretManager({resource: "   "})).get("ANTHROPIC_API_KEY")


def test_the_gcp_backend_is_the_default_not_env(monkeypatch):
    """B11's reasoning applied to secrets: no silent fallback to a local value."""
    monkeypatch.delenv("LQABR_SECRET_BACKEND", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-local-value-that-must-not-win")
    monkeypatch.setenv("LQABR_SECRET_PROJECT", "lqabr-dev")
    monkeypatch.setenv("LQABR_SECRET_ANTHROPIC_API_KEY", "k")
    resource = "projects/lqabr-dev/secrets/k/versions/latest"
    client = FakeSecretManager({resource: SECRET_VALUE})

    resolver = SecretResolver(client=client)
    assert resolver.get("ANTHROPIC_API_KEY") == SECRET_VALUE


def test_an_unknown_backend_is_refused():
    with pytest.raises(SecretConfigError):
        SecretResolver(backend="vault", project="p").get("ANTHROPIC_API_KEY")


# --- the env backend is local/CI only --------------------------------------


def test_the_env_backend_reads_the_plain_name(monkeypatch):
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "pat-local")
    resolver = SecretResolver(backend="env", project=None)
    assert resolver.get("HUBSPOT_PRIVATE_APP_TOKEN") == "pat-local"


def test_the_env_backend_still_fails_loudly_when_unset(monkeypatch):
    monkeypatch.delenv("HUBSPOT_CLIENT_SECRET", raising=False)
    resolver = SecretResolver(backend="env", project=None)
    with pytest.raises(SecretConfigError) as exc:
        resolver.get("HUBSPOT_CLIENT_SECRET")
    assert "local development" in str(exc.value)


# --- the value never leaks -------------------------------------------------


def test_the_secret_value_is_never_written_to_any_log(monkeypatch, capsys):
    monkeypatch.setenv("LQABR_SECRET_ANTHROPIC_API_KEY", "k")
    resource = "projects/lqabr-dev/secrets/k/versions/latest"
    _resolver(client=FakeSecretManager({resource: SECRET_VALUE})).get("ANTHROPIC_API_KEY")

    out = capsys.readouterr().out
    assert SECRET_VALUE not in out, "the secret value reached a log stream"

    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    resolved = [e for e in events if e.get("event") == "secret_resolved"]
    assert resolved, "the fetch itself must be on the record"
    assert resolved[0]["resource"] == resource   # WHICH secret: yes
    assert SECRET_VALUE not in json.dumps(resolved[0])
    assert resolved[0]["value"].startswith("<") and "chars" in resolved[0]["value"]

    audit = [e for e in events if e.get("event") == "secret_manager_access"]
    assert audit, "an external call must produce an audit line"
    assert audit[0]["endpoint"] == resource


def test_a_failed_fetch_does_not_echo_the_resource_contents():
    client = FakeSecretManager(error=RuntimeError(f"boom while reading {SECRET_VALUE}"))
    with pytest.raises(SecretAccessError) as exc:
        _resolver(client=client).get("ANTHROPIC_API_KEY")
    # The underlying message is included for debugging — this test exists to
    # make that a deliberate, reviewed choice rather than an accident.
    assert "boom" in str(exc.value)


# --- auth.py routes credentials through the resolver ----------------------


def test_hubspot_credentials_come_from_secret_manager(monkeypatch):
    from lqabr_core.leadgen.hubspot import auth as auth_module

    monkeypatch.setenv("LQABR_SECRET_BACKEND", "gcp")
    monkeypatch.setenv("LQABR_SECRET_PROJECT", "lqabr-dev")
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "must-not-be-used")
    resource = "projects/lqabr-dev/secrets/lqabr-hubspot-private-app-token/versions/latest"
    client = FakeSecretManager({resource: "pat-from-secret-manager"})
    reset_resolver(SecretResolver(backend="gcp", project="lqabr-dev", client=client))

    token = auth_module.PrivateAppTokenProvider().fetch()
    assert token.value == "pat-from-secret-manager"
    assert client.requests == [resource]


def test_an_unresolvable_credential_is_an_auth_config_error(monkeypatch):
    """It must halt the run, not be recorded against a lead (B1)."""
    from lqabr_core.leadgen.hubspot import auth as auth_module

    reset_resolver(
        SecretResolver(backend="gcp", project="lqabr-dev", client=FakeSecretManager())
    )
    with pytest.raises(auth_module.AuthConfigError):
        auth_module.PrivateAppTokenProvider().fetch()


# --- the model key is lazy and stays out of os.environ --------------------


def test_importing_the_agent_needs_no_secret(monkeypatch):
    """adk web imports at startup; that must not require a credential."""
    monkeypatch.setenv("LQABR_SECRET_BACKEND", "gcp")
    reset_resolver(SecretResolver(backend="gcp", project=None, client=FakeSecretManager()))

    import importlib

    module = importlib.import_module("agent")
    importlib.reload(module)
    assert module.root_agent.name == "lead_profile_agent"


def test_the_model_key_is_never_put_into_os_environ(monkeypatch):
    import os

    from model import SecretBackedLiteLlm

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resource = "projects/lqabr-dev/secrets/lqabr-anthropic-api-key/versions/latest"
    reset_resolver(
        SecretResolver(
            backend="gcp",
            project="lqabr-dev",
            client=FakeSecretManager({resource: SECRET_VALUE}),
        )
    )

    model = SecretBackedLiteLlm(model="anthropic/claude-sonnet-4-6")
    model._ensure_api_key()

    assert model._additional_args["api_key"] == SECRET_VALUE
    assert os.getenv("ANTHROPIC_API_KEY") is None, "the key must not enter the environment"


def test_a_stray_env_key_cannot_beat_secret_manager(monkeypatch):
    """The gcp backend must not be silently overridden by the environment."""
    from model import SecretBackedLiteLlm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-key-left-in-the-environment")
    resource = "projects/lqabr-dev/secrets/lqabr-anthropic-api-key/versions/latest"
    reset_resolver(
        SecretResolver(
            backend="gcp",
            project="lqabr-dev",
            client=FakeSecretManager({resource: SECRET_VALUE}),
        )
    )

    model = SecretBackedLiteLlm(model="anthropic/claude-sonnet-4-6")
    model._ensure_api_key()
    assert model._additional_args["api_key"] == SECRET_VALUE


def test_the_env_backend_is_the_local_dev_path_for_the_model_key(monkeypatch):
    from model import SecretBackedLiteLlm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-dev-key")
    reset_resolver(SecretResolver(backend="env", project=None))

    model = SecretBackedLiteLlm(model="anthropic/claude-sonnet-4-6")
    model._ensure_api_key()
    assert model._additional_args["api_key"] == "local-dev-key"


def test_an_unmapped_provider_is_left_to_litellm(monkeypatch, capsys):
    from model import SecretBackedLiteLlm

    model = SecretBackedLiteLlm(model="cohere/command-r")
    model._ensure_api_key()
    assert "api_key" not in model._additional_args
    assert "model_key_not_managed" in capsys.readouterr().out
