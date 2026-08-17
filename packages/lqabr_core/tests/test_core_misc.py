import os
import pytest

from lqabr_core.secrets import SecretNotFoundError, get_secret
from lqabr_core.timezones import SUPPORTED_ZONES, to_iana, zone_options
from lqabr_core.types import LeadProfile


def test_get_secret_env_fallback(monkeypatch):
    get_secret.cache_clear()
    monkeypatch.setenv("LQABR_HUBSPOT_ACCESS_TOKEN", "tok-123")
    assert get_secret("lqabr-hubspot-access-token") == "tok-123"


def test_get_secret_missing_raises(monkeypatch):
    get_secret.cache_clear()
    monkeypatch.delenv("LQABR_NOPE_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(SecretNotFoundError):
        get_secret("lqabr-nope-secret")
    get_secret.cache_clear()


def test_all_four_scheduling_zones_offered():
    options = zone_options()
    assert [z.label for z in options] == ["EST", "CST", "PST", "IST"]
    assert {z.iana for z in options} == set(SUPPORTED_ZONES.values())


def test_to_iana_accepts_labels_and_iana_names():
    assert to_iana("ist") == "Asia/Kolkata"
    assert to_iana("America/New_York") == "America/New_York"
    with pytest.raises(Exception):
        to_iana("Not/AZone")


def test_lead_profile_pointers_and_contactability():
    lead = LeadProfile(full_name="A B", email="a@b.c")
    assert set(lead.pointers()) == set(LeadProfile.POINTER_FIELDS)
    assert lead.is_contactable
    assert "phone" in lead.missing_pointers()
    assert not LeadProfile(full_name="No Channels").is_contactable


# --------------------------------------------------- secret source selection
def _fresh():
    from lqabr_core.secrets import get_secret
    get_secret.cache_clear()
    return get_secret


def test_auto_prefers_the_environment_which_is_how_set_secrets_arrives(monkeypatch):
    """Cloud Run --set-secrets reads Secret Manager at deploy and injects the
    value as an env var, so env-first IS Secret Manager in a real service."""
    monkeypatch.setenv("LQABR_SECRETS_SOURCE", "auto")
    monkeypatch.setenv("LQABR_HUBSPOT_ACCESS_TOKEN", "from-set-secrets")
    assert _fresh()("lqabr-hubspot-access-token") == "from-set-secrets"


def test_secret_manager_mode_ignores_the_environment_entirely(monkeypatch):
    """The point of this mode: prove a value came from Secret Manager. A
    literal left in a .env must NOT be able to satisfy it."""
    import sys
    import types

    monkeypatch.setenv("LQABR_SECRETS_SOURCE", "secret_manager")
    monkeypatch.setenv("LQABR_HUBSPOT_ACCESS_TOKEN", "a-literal-in-dot-env")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")

    calls = {}

    class FakeClient:
        def access_secret_version(self, request):
            calls["name"] = request["name"]
            payload = types.SimpleNamespace(data=b"from-secret-manager")
            return types.SimpleNamespace(payload=payload)

    # google-cloud-secret-manager is an optional extra and is not installed in
    # the test environment, so stand the module tree up rather than skipping —
    # the branch under test is exactly the one that only runs when it IS.
    fake_sm = types.ModuleType("google.cloud.secretmanager")
    fake_sm.SecretManagerServiceClient = lambda *a, **k: FakeClient()
    google_mod = sys.modules.get("google") or types.ModuleType("google")
    cloud_mod = types.ModuleType("google.cloud")
    cloud_mod.secretmanager = fake_sm
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_sm)

    assert _fresh()("lqabr-hubspot-access-token") == "from-secret-manager"
    assert calls["name"] == "projects/proj/secrets/lqabr-hubspot-access-token/versions/latest"


def test_env_mode_never_calls_the_api(monkeypatch):
    monkeypatch.setenv("LQABR_SECRETS_SOURCE", "env")
    monkeypatch.delenv("LQABR_HUBSPOT_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    from lqabr_core.secrets import SecretNotFoundError
    with pytest.raises(SecretNotFoundError) as exc:
        _fresh()("lqabr-hubspot-access-token")
    assert "forbids the Secret Manager API" in str(exc.value)


def test_an_unknown_source_fails_loudly(monkeypatch):
    monkeypatch.setenv("LQABR_SECRETS_SOURCE", "wherever")
    from lqabr_core.secrets import SecretNotFoundError
    with pytest.raises(SecretNotFoundError):
        _fresh()("lqabr-hubspot-access-token")


def test_the_source_is_logged_but_never_the_value(monkeypatch, caplog):
    """Before this you could not tell whether a running service was using a
    Secret Manager value or a literal someone left in a .env."""
    import logging
    monkeypatch.setenv("LQABR_SECRETS_SOURCE", "auto")
    monkeypatch.setenv("LQABR_HUBSPOT_ACCESS_TOKEN", "pat-super-secret")
    with caplog.at_level(logging.INFO, logger="lqabr.secrets"):
        _fresh()("lqabr-hubspot-access-token")
    text = caplog.text
    assert "resolved from environment" in text
    assert "lqabr-hubspot-access-token" in text
    assert "pat-super-secret" not in text


# ------------------------------------------ provider keys from Secret Manager
def test_an_anthropic_model_gets_its_key_from_secret_manager(monkeypatch):
    """litellm reads ANTHROPIC_API_KEY from the environment and knows nothing
    about Secret Manager. Without this the only way to supply it is a literal
    in .env — the exact thing we are removing."""
    from lqabr_core import model as model_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(model_mod, "get_secret",
                        lambda name: "sk-ant-from-secret-manager"
                        if name == "lqabr-anthropic-api-key" else "")

    assert model_mod.ensure_provider_credentials("anthropic/claude-sonnet-5") == "ANTHROPIC_API_KEY"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-secret-manager"


def test_a_key_already_in_the_environment_is_left_alone(monkeypatch):
    """--set-secrets injects it as an env var; that value already came from
    Secret Manager, so do not spend an API call replacing it."""
    from lqabr_core import model as model_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "already-injected")
    called = []
    monkeypatch.setattr(model_mod, "get_secret", lambda name: called.append(name) or "x")

    assert model_mod.ensure_provider_credentials("anthropic/claude-sonnet-5") is None
    assert called == []


def test_vertex_needs_no_api_key(monkeypatch):
    from lqabr_core import model as model_mod

    monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "1")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(model_mod, "get_secret", lambda name: called.append(name) or "x")

    assert model_mod.ensure_provider_credentials("gemini-2.0-flash") is None
    assert called == []          # ADC, not a key


def test_a_missing_provider_secret_warns_rather_than_crashing(monkeypatch):
    """Failing here would take the container down at import for a credential
    that may never be used."""
    from lqabr_core import model as model_mod
    from lqabr_core.secrets import SecretNotFoundError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def boom(name):
        raise SecretNotFoundError("no such secret")

    monkeypatch.setattr(model_mod, "get_secret", boom)
    assert model_mod.ensure_provider_credentials("anthropic/claude-sonnet-5") is None


def test_an_unknown_provider_is_a_no_op(monkeypatch):
    from lqabr_core import model as model_mod
    assert model_mod.ensure_provider_credentials("someprovider/some-model") is None


def test_absent_ADC_becomes_SecretNotFoundError_not_a_raw_google_error(monkeypatch):
    """The client CONSTRUCTOR resolves ADC and raises when there is none. If
    that escapes un-normalised, every caller that does not wrap get_secret
    itself returns an opaque 500 — which is what the Mailgun webhook did."""
    import sys
    import types

    monkeypatch.setenv("LQABR_SECRETS_SOURCE", "secret_manager")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")

    class DefaultCredentialsError(Exception):
        pass

    def exploding_client(*a, **k):
        raise DefaultCredentialsError("Your default credentials were not found.")

    fake_sm = types.ModuleType("google.cloud.secretmanager")
    fake_sm.SecretManagerServiceClient = exploding_client
    cloud_mod = types.ModuleType("google.cloud")
    cloud_mod.secretmanager = fake_sm
    monkeypatch.setitem(sys.modules, "google", sys.modules.get("google") or types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_sm)

    with pytest.raises(SecretNotFoundError) as exc:
        _fresh()("lqabr-mailgun-webhook-signing-key")
    assert "secretmanager.secretAccessor" in str(exc.value)
