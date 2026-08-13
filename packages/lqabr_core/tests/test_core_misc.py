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
