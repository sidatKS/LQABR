"""Tests for the Vapi end-of-call report relay.

The relay is the gateway's only authenticating proxy, and it is the component
that owns Vapi authenticity — txtv's /call-report handler verifies nothing. So
the security-relevant behaviour is tested here rather than assumed:

  * a wrong / missing secret never reaches the agent
  * the body is forwarded byte-for-byte, unchanged
  * a missing hubspot_contact_id is NOT an error (txtv falls back to a phone
    lookup, and rejecting the report would lose the call outcome for good)
  * the transcript never enters an audit record
"""

from __future__ import annotations

import json

import pytest

from conftest import gw_call_report as cr  # noqa: E402


# --------------------------------------------------------------- fake transport
class _Response:
    def __init__(self, status_code=200, content=b'{"ok":true}',
                 content_type="application/json"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class _Session:
    """Records what the relay sent, so the test can assert on the exact bytes."""

    def __init__(self, response=None, raises=None):
        self.calls = []
        self._response = response or _Response()
        self._raises = raises

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers or {},
                           "timeout": timeout})
        if self._raises:
            raise self._raises
        return self._response

    def get(self, *a, **kw):  # metadata server — never reachable in tests
        raise cr.requests.RequestException("no metadata server in tests")


REPORT = {
    "message": {
        "type": "end-of-call-report",
        "endedReason": "customer-ended-call",
        "call": {
            "id": "call-abc",
            "assistantOverrides": {
                "variableValues": {"hubspot_contact_id": "523828708059"}},
        },
        "artifact": {"transcript": "AI: Hello there. User: Not interested."},
    }
}
RAW = json.dumps(REPORT).encode()


def _relay(session=None, secret="s3cret", verify=True):
    return cr.CallReportRelay(
        target_url="http://text-voice.test/call-report",
        secret=secret, verify_secret=verify,
        session=session or _Session(),
        token_provider=cr.IdTokenProvider("", session=_Session()),
    )


# ------------------------------------------------------------------- the secret
class TestSecret:
    def test_correct_secret_in_header_is_accepted(self):
        session = _Session()
        status, _, _ = _relay(session).forward(RAW, {"x-vapi-secret": "s3cret"})
        assert status == 200
        assert len(session.calls) == 1

    def test_secret_may_arrive_as_a_bearer_token(self):
        """Vapi can be configured either way; both must work."""
        session = _Session()
        status, _, _ = _relay(session).forward(RAW, {"authorization": "Bearer s3cret"})
        assert status == 200

    def test_wrong_secret_is_401_and_never_reaches_the_agent(self):
        session = _Session()
        with pytest.raises(cr.VapiSecretError) as exc:
            _relay(session).forward(RAW, {"x-vapi-secret": "wrong"})
        assert exc.value.status_code == 401
        assert session.calls == []  # the whole point

    def test_missing_secret_header_is_401(self):
        session = _Session()
        with pytest.raises(cr.VapiSecretError):
            _relay(session).forward(RAW, {})
        assert session.calls == []

    def test_unconfigured_secret_fails_closed(self):
        """No secret configured must reject, not wave everything through.

        The endpoint is public and txtv does not check — failing open here
        would make it an unauthenticated write path into the CRM.
        """
        session = _Session()
        with pytest.raises(cr.VapiSecretError):
            _relay(session, secret="").forward(RAW, {"x-vapi-secret": "anything"})
        assert session.calls == []

    def test_verification_can_be_disabled_for_local_testing(self):
        session = _Session()
        status, _, _ = _relay(session, secret="", verify=False).forward(RAW, {})
        assert status == 200


# ------------------------------------------------------------------ the forward
class TestForward:
    def test_body_is_forwarded_byte_for_byte(self):
        """txtv accepts either the wrapped envelope or the bare message and
        keys off message.type — re-serialising risks changing a shape we do
        not own."""
        session = _Session()
        _relay(session).forward(RAW, {"x-vapi-secret": "s3cret"})
        assert session.calls[0]["data"] == RAW

    def test_agent_status_and_body_are_returned_verbatim(self):
        """Vapi's retry logic must see txtv's real outcome, not ours."""
        session = _Session(_Response(status_code=422, content=b'{"detail":"nope"}'))
        status, body, _ = _relay(session).forward(RAW, {"x-vapi-secret": "s3cret"})
        assert (status, body) == (422, b'{"detail":"nope"}')

    def test_unreachable_agent_raises_502_so_vapi_retries(self):
        session = _Session(raises=cr.requests.RequestException("connection refused"))
        with pytest.raises(cr.ReportRelayError) as exc:
            _relay(session).forward(RAW, {"x-vapi-secret": "s3cret"})
        assert exc.value.status_code == 502

    def test_unset_target_is_503_not_a_silent_success(self):
        relay = cr.CallReportRelay(target_url="", secret="s3cret", session=_Session())
        with pytest.raises(cr.ReportRelayError) as exc:
            relay.forward(RAW, {"x-vapi-secret": "s3cret"})
        assert exc.value.status_code == 503

    def test_no_id_token_off_platform_is_not_fatal(self):
        """A developer laptop has no metadata server. The relay still forwards
        (correct against a local stub); on Cloud Run a missing token surfaces
        as a visible 403 from txtv rather than silence."""
        session = _Session()
        _relay(session).forward(RAW, {"x-vapi-secret": "s3cret"})
        assert "Authorization" not in session.calls[0]["headers"]


# -------------------------------------------------------------- correlation ids
class TestCorrelation:
    def test_wrapped_envelope(self):
        ids = cr.extract_correlation(REPORT)
        assert ids == {"call_id": "call-abc", "hubspot_contact_id": "523828708059"}

    def test_bare_message_without_the_message_wrapper(self):
        ids = cr.extract_correlation(REPORT["message"])
        assert ids["call_id"] == "call-abc"
        assert ids["hubspot_contact_id"] == "523828708059"

    def test_contact_id_from_metadata_fallback(self):
        ids = cr.extract_correlation(
            {"message": {"call": {"id": "c1"}, "metadata": {"hubspot_contact_id": "77"}}})
        assert ids["hubspot_contact_id"] == "77"

    def test_absent_contact_id_is_none_not_an_error(self):
        """txtv falls back to a phone lookup. Rejecting here would throw away
        a completed call's outcome permanently."""
        ids = cr.extract_correlation({"message": {"call": {"id": "c1"}}})
        assert ids == {"call_id": "c1", "hubspot_contact_id": None}

    @pytest.mark.parametrize("payload", [None, "a string", 42, [], {}])
    def test_malformed_payloads_do_not_raise(self, payload):
        assert cr.extract_correlation(payload) == {
            "call_id": None, "hubspot_contact_id": None}

    def test_transcript_is_never_extracted(self):
        """Only ids leave this function. The audit hooks scan every record for
        profile fields, and a real conversation would trip ProfileFieldLeak."""
        ids = cr.extract_correlation(REPORT)
        assert set(ids) == {"call_id", "hubspot_contact_id"}
        assert "Not interested" not in json.dumps(ids)
