import json

import pytest

from zoom_client import ZoomError, ZoomSchedulerClient


class FakeResponse:
    def __init__(self, status_code=200, body=None):
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
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


def make_client(responses):
    session = FakeSession(responses)
    return ZoomSchedulerClient(account_id="a", client_id="c", client_secret="s",
                               session=session), session


def test_booking_link_uses_env_override(monkeypatch):
    monkeypatch.setenv("ZOOM_BOOKING_URL", "https://scheduler.zoom.us/team/intro")
    client, session = make_client([])
    assert client.booking_link() == "https://scheduler.zoom.us/team/intro"
    assert session.calls == []


def test_booking_link_falls_back_to_first_schedule(monkeypatch):
    monkeypatch.delenv("ZOOM_BOOKING_URL", raising=False)
    client, session = make_client([
        FakeResponse(200, {"access_token": "at", "expires_in": 3600}),
        FakeResponse(200, {"schedules": [{"schedule_id": "s1",
                                          "share_link": "https://scheduler.zoom.us/x/s1"}]}),
    ])
    assert client.booking_link() == "https://scheduler.zoom.us/x/s1"
    assert session.calls[0][0] == "POST"       # OAuth first
    assert "/scheduler/schedules" in session.calls[1][1]


def test_no_schedules_raises(monkeypatch):
    monkeypatch.delenv("ZOOM_BOOKING_URL", raising=False)
    client, _ = make_client([
        FakeResponse(200, {"access_token": "at", "expires_in": 3600}),
        FakeResponse(200, {"schedules": []}),
    ])
    with pytest.raises(ZoomError, match="no Zoom Scheduler schedules"):
        client.booking_link()


def test_oauth_failure_raises():
    client, _ = make_client([FakeResponse(401, {"reason": "bad"})])
    with pytest.raises(ZoomError, match="OAuth failed"):
        client.list_schedules()
