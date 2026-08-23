"""The input abstraction: four kinds in, one NormalizedDocument out."""

from __future__ import annotations

import pytest

from helpers import FakeResponse, FakeSession, html_page
from summary_core import sources
from summary_core.settings import Settings
from summary_core.sources.base import guard_url, select_path
from summary_core.types import NormalizedDocument, SourceError, SourceSpec


@pytest.fixture
def settings():
    # Public DNS is not reachable from the test box, so allowlist the host
    # under test rather than letting the guard resolve it.
    return Settings(allowed_hosts=["example.com", "internal.svc"], max_chars=10_000, max_retries=3)


# ---------------------------------------------------------------- spec
class TestSourceSpec:
    def test_all_four_kinds_are_registered(self):
        assert sources.available_kinds() == ("api", "json", "text", "url")

    def test_unknown_kind_is_refused_by_name(self):
        with pytest.raises(SourceError, match="unknown source kind"):
            SourceSpec(kind="carrier-pigeon")

    def test_kind_is_inferred_from_the_field_present(self):
        assert SourceSpec.from_dict({"url": "https://example.com/x"}).kind == "url"
        assert SourceSpec.from_dict({"payload": {"a": 1}}).kind == "json"
        assert SourceSpec.from_dict({"endpoint": "https://example.com/api"}).kind == "api"
        assert SourceSpec.from_dict({"text": "hello"}).kind == "text"

    def test_a_bare_string_is_a_url_or_text(self):
        assert SourceSpec.from_dict("https://example.com/x").kind == "url"
        assert SourceSpec.from_dict("just some prose").kind == "text"

    @pytest.mark.parametrize("value", ["file:///etc/passwd", "ftp://host/x", "gopher://x/1"])
    def test_a_bare_string_with_any_scheme_is_a_url_so_the_guard_sees_it(self, value):
        """Reading these as prose would summarise the literal string and hide
        the caller's mistake. They must reach the guard and be refused."""
        spec = SourceSpec.from_dict(value)
        assert spec.kind == "url"
        with pytest.raises(SourceError, match="only http and https"):
            sources.fetch(spec, Settings())

    def test_missing_required_field_is_named(self):
        with pytest.raises(SourceError, match="requires 'endpoint'"):
            SourceSpec(kind="api")

    def test_reference_never_leaks_a_payload(self):
        spec = SourceSpec(kind="json", payload={"ssn": "123-45-6789"})
        assert "123-45" not in spec.reference


# ---------------------------------------------------------------- guard
class TestUrlGuard:
    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x"])
    def test_only_http_and_https(self, url, settings):
        with pytest.raises(SourceError, match="only http and https"):
            guard_url(url, settings)

    def test_host_outside_the_allowlist_is_refused(self, settings):
        with pytest.raises(SourceError, match="not in LQABR_SUMMARY_ALLOWED_HOSTS"):
            guard_url("https://evil.test/x", settings)

    def test_subdomains_of_an_allowlisted_host_pass(self, settings):
        assert guard_url("https://blog.example.com/post", settings)

    @pytest.mark.parametrize("host,address", [
        ("metadata.test", "169.254.169.254"),   # the GCP metadata server
        ("localhost.test", "127.0.0.1"),
        ("internal.test", "10.1.2.3"),
    ])
    def test_non_public_addresses_are_refused(self, monkeypatch, host, address):
        """No allowlist -> the guard resolves and refuses private targets.

        169.254.169.254 is the one that matters: it hands out service-account
        tokens to anything that can make it issue a GET.
        """
        monkeypatch.setattr(
            "summary_core.sources.base.socket.getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", (address, 443))],
        )
        with pytest.raises(SourceError, match="non-public address"):
            guard_url(f"https://{host}/x", Settings())

    def test_private_host_allowed_when_explicitly_enabled(self):
        assert guard_url("http://localhost:8080/x", Settings(allow_private_hosts=True))


# ---------------------------------------------------------------- url
class TestUrlAdapter:
    def test_extracts_the_article_and_drops_the_furniture(self, settings):
        session = FakeSession([FakeResponse(text=html_page(), headers={"Content-Type": "text/html"})])
        doc = sources.fetch(SourceSpec(kind="url", url="https://example.com/post"),
                            settings, session=session)
        assert isinstance(doc, NormalizedDocument)
        assert doc.source_kind == "url"
        assert doc.title == "A Post"
        assert "First line." in doc.text
        for noise in ("tracking", "Home About Contact", "Related posts", "(c) Example"):
            assert noise not in doc.text

    def test_retries_a_503_then_succeeds(self, settings):
        session = FakeSession([
            FakeResponse(status_code=503, text="busy"),
            FakeResponse(text=html_page(), headers={"Content-Type": "text/html"}),
        ])
        doc = sources.fetch(SourceSpec(kind="url", url="https://example.com/post"),
                            settings, session=session)
        assert len(session.calls) == 2
        assert doc.title == "A Post"

    def test_a_404_is_an_error_not_an_empty_summary(self, settings):
        session = FakeSession([FakeResponse(status_code=404, text="nope")])
        with pytest.raises(SourceError, match="HTTP 404"):
            sources.fetch(SourceSpec(kind="url", url="https://example.com/gone"),
                          settings, session=session)

    def test_long_pages_are_truncated_and_say_so(self):
        settings = Settings(allowed_hosts=["example.com"], max_chars=50)
        session = FakeSession([FakeResponse(text=html_page(body="x " * 500),
                                            headers={"Content-Type": "text/html"})])
        doc = sources.fetch(SourceSpec(kind="url", url="https://example.com/post"),
                            settings, session=session)
        assert doc.truncated is True
        assert doc.char_count == 50

    def test_a_page_with_no_text_is_refused(self, settings):
        session = FakeSession([FakeResponse(text="<html><body></body></html>",
                                            headers={"Content-Type": "text/html"})])
        with pytest.raises(SourceError, match="no readable text"):
            sources.fetch(SourceSpec(kind="url", url="https://example.com/empty"),
                          settings, session=session)


# ---------------------------------------------------------------- json
class TestJsonAdapter:
    def test_any_shape_becomes_a_stable_document(self, settings):
        payload = {"title": "Q3 pipeline", "b": 2, "a": [1, 2, 3]}
        doc = sources.fetch(SourceSpec(kind="json", payload=payload), settings)
        assert doc.source_kind == "json"
        assert doc.title == "Q3 pipeline"
        assert doc.text.index('"a"') < doc.text.index('"b"'), "keys are sorted for stability"

    def test_select_narrows_the_payload(self, settings):
        payload = {"data": {"items": [{"body": "the actual content"}]}}
        doc = sources.fetch(
            SourceSpec(kind="json", payload=payload, select="$.data.items[0].body"), settings)
        assert doc.text == "the actual content"

    def test_a_bad_select_is_named_not_silently_empty(self, settings):
        with pytest.raises(SourceError, match="no key"):
            sources.fetch(SourceSpec(kind="json", payload={"a": 1}, select="$.b.c"), settings)

    def test_no_title_is_left_empty_never_invented(self, settings):
        doc = sources.fetch(SourceSpec(kind="json", payload={"body": "text"}), settings)
        assert doc.title == ""


def test_select_path_passthrough_when_no_expression():
    assert select_path({"a": 1}, None) == {"a": 1}


# ---------------------------------------------------------------- api
class TestApiAdapter:
    def test_json_endpoint(self, settings):
        session = FakeSession([FakeResponse(headers={"Content-Type": "application/json"},
                                            _json={"summary_title": "Report", "body": "x"})])
        doc = sources.fetch(SourceSpec(kind="api", endpoint="https://internal.svc/report"),
                            settings, session=session)
        assert doc.title == "Report"
        assert doc.metadata["method"] == "GET"

    def test_html_endpoint_goes_through_the_same_extractor(self, settings):
        session = FakeSession([FakeResponse(text=html_page("Docs"),
                                            headers={"Content-Type": "text/html; charset=utf-8"})])
        doc = sources.fetch(SourceSpec(kind="api", endpoint="https://internal.svc/docs"),
                            settings, session=session)
        assert doc.title == "Docs"
        assert "tracking" not in doc.text

    def test_post_with_headers_and_body_is_passed_through(self, settings):
        session = FakeSession([FakeResponse(headers={"Content-Type": "application/json"},
                                            _json={"ok": True})])
        sources.fetch(
            SourceSpec(kind="api", endpoint="https://internal.svc/q", method="POST",
                       headers={"Authorization": "Bearer t"}, body={"q": "leads"}),
            settings, session=session)
        call = session.calls[0]
        assert call["method"] == "POST"
        assert call["json"] == {"q": "leads"}
        assert call["headers"]["Authorization"] == "Bearer t"

    def test_plain_text_endpoint(self, settings):
        session = FakeSession([FakeResponse(text="just words",
                                            headers={"Content-Type": "text/plain"})])
        doc = sources.fetch(SourceSpec(kind="api", endpoint="https://internal.svc/t"),
                            settings, session=session)
        assert doc.text == "just words"


# ---------------------------------------------------------------- text
def test_text_adapter_passes_through(settings):
    doc = sources.fetch(SourceSpec(kind="text", text="hello world",
                                   options={"title": "Note"}), settings)
    assert (doc.title, doc.text, doc.truncated) == ("Note", "hello world", False)
