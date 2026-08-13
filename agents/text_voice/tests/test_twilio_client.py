import json

import pytest

from twilio_client import TwilioClient, TwilioError, validate_twilio_signature


class FakeResponse:
    def __init__(self, status_code=201, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def make_client(responses):
    session = FakeSession(responses)
    client = TwilioClient(account_sid="AC1", auth_token="tok",
                          from_number="+15550000000", session=session)
    return client, session


def test_send_sms_posts_message():
    client, session = make_client([FakeResponse(201, {"sid": "SM1"})])
    result = client.send_sms("+15551112222", "hello", status_callback="https://cb.example/s")
    assert result["sid"] == "SM1"
    data = session.calls[0][1]["data"]
    assert data["To"] == "+15551112222"
    assert data["StatusCallback"] == "https://cb.example/s"


def test_place_call_enables_machine_detection():
    client, session = make_client([FakeResponse(201, {"sid": "CA1"})])
    client.place_call("+15551112222", "https://cb.example/answer")
    data = session.calls[0][1]["data"]
    assert data["MachineDetection"] == "DetectMessageEnd"
    assert data["Url"] == "https://cb.example/answer"


def test_client_error_raises_without_retry():
    client, session = make_client([FakeResponse(400, {"message": "bad number"})])
    with pytest.raises(TwilioError, match="HTTP 400"):
        client.send_sms("+bad", "x")
    assert len(session.calls) == 1


def test_signature_validation_round_trip():
    import base64
    import hashlib
    import hmac

    url = "https://x.example/voice/answer?contact_id=1"
    params = {"CallSid": "CA1", "AnsweredBy": "human"}
    payload = url + "".join(k + params[k] for k in sorted(params))
    good = base64.b64encode(hmac.new(b"tok", payload.encode(), hashlib.sha1).digest()).decode()

    assert validate_twilio_signature(url, params, good, auth_token="tok")
    assert not validate_twilio_signature(url, params, "forged", auth_token="tok")
