"""The Mailgun tool call — "give me the status of these track IDs".

One pull, on demand, with no loop. Local to the email agent (mailgun_tool),
not a root-level MCP folder.
"""

import pytest

from lqabr_core.mailgun import MailgunError
from mailgun_tool import TOOLS, MailgunEventsClient, fetch_event_status


class RecordingObs:
    def __init__(self):
        self.audits = []
        self.processes = []

    def audit(self, **fields):
        self.audits.append(fields)

    def process(self, **fields):
        self.processes.append(fields)


class FakeGetSession:
    """Records GETs and replays queued responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            return FakeResp(200, {"items": []})
        return self.responses.pop(0)


class FakeResp:
    def __init__(self, status_code=200, json_body=None, text=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text if text is not None else "body"

    def json(self):
        return self._json


def event(message_id="<m-1@mg>", name="delivered", token="trg-1:run-1"):
    return {"event": name, "id": "evt-" + message_id,
            "message": {"headers": {"message-id": message_id}},
            "user-variables": {"lqabr_correlation_token": token}}


def client(responses, obs=None, session=None):
    return MailgunEventsClient(api_key="key", domain="mg.example.com",
                               session=session or FakeGetSession(responses),
                               obs=obs or RecordingObs(), backoff_seconds=0)


# ------------------------------------------------------------------ the pull
def test_one_pull_returns_the_runs_events():
    events = client([FakeResp(200, {"items": [event("<m-1@mg>"), event("<m-2@mg>")]})]
                    ).fetch_events(correlation_token="trg-1:run-1")
    assert len(events) == 2


def test_the_correlation_token_is_not_sent_as_a_server_side_param():
    """Mailgun's events API only accepts its own documented filter fields, so
    a custom user variable on the query string is rejected with HTTP 400
    ("Unknown parameter"). The correlation token must therefore never go on
    the wire as a filter — the run is matched client-side instead."""
    session = FakeGetSession([FakeResp(200, {"items": []})])
    MailgunEventsClient(api_key="k", domain="mg.example.com", session=session,
                        obs=RecordingObs(), backoff_seconds=0
                        ).fetch_events(correlation_token="trg-1:run-9")

    params = session.calls[0]["params"]
    assert "lqabr_correlation_token" not in params
    assert "events" in session.calls[0]["url"]


def test_a_single_message_id_narrows_server_side():
    """One wanted message uses Mailgun's supported `message-id` filter so the
    response is scoped to that message rather than the whole domain."""
    session = FakeGetSession([FakeResp(200, {"items": [event("<only@mg>")]})])
    MailgunEventsClient(api_key="k", domain="mg.example.com", session=session,
                        obs=RecordingObs(), backoff_seconds=0
                        ).fetch_events(message_ids=["<only@mg>"])

    assert session.calls[0]["params"]["message-id"] == "only@mg"


def test_ascending_is_not_forced_without_a_time_anchor():
    """`ascending=yes` with no `begin` makes Mailgun anchor at 'now' and search
    forward, returning zero events for a message already sent. It must only be
    sent alongside a begin anchor."""
    session = FakeGetSession([FakeResp(200, {"items": []})])
    MailgunEventsClient(api_key="k", domain="mg.example.com", session=session,
                        obs=RecordingObs(), backoff_seconds=0
                        ).fetch_events(message_ids=["<only@mg>"])
    assert "ascending" not in session.calls[0]["params"]


def test_ascending_rides_along_with_a_begin_anchor():
    session = FakeGetSession([FakeResp(200, {"items": []})])
    MailgunEventsClient(api_key="k", domain="mg.example.com", session=session,
                        obs=RecordingObs(), backoff_seconds=0
                        ).fetch_events(message_ids=["<only@mg>"], begin="1785849598")
    params = session.calls[0]["params"]
    assert params["begin"] == "1785849598" and params["ascending"] == "yes"


def test_message_ids_narrow_the_result():
    """Mailgun cannot filter on a message-id list, so that part is
    client-side — but the caller must still only get its own run's messages."""
    session = FakeGetSession([FakeResp(200, {"items": [
        event("<mine@mg>"), event("<someone-elses@mg>")]})])
    events = client(None, session=session).fetch_events(message_ids=["<mine@mg>"])
    assert [e["message"]["headers"]["message-id"] for e in events] == ["<mine@mg>"]


def test_angle_brackets_do_not_break_matching():
    session = FakeGetSession([FakeResp(200, {"items": [event("<m-1@mg>")]})])
    assert client(None, session=session).fetch_events(message_ids=["m-1@mg"])


def test_pagination_is_followed():
    session = FakeGetSession([
        FakeResp(200, {"items": [event("<m-1@mg>")],
                       "paging": {"next": "https://api.mailgun.net/v3/mg/events?page=2"}}),
        FakeResp(200, {"items": [event("<m-2@mg>")]}),
    ])
    assert len(client(None, session=session).fetch_events()) == 2
    assert len(session.calls) == 2


def test_an_empty_page_ends_the_walk_rather_than_looping():
    """A `next` cursor that keeps returning nothing must not spin."""
    session = FakeGetSession([
        FakeResp(200, {"items": [], "paging": {"next": "https://api.mailgun.net/v3/mg/e?p=2"}}),
    ])
    assert client(None, session=session).fetch_events() == []
    assert len(session.calls) == 1


def test_the_page_walk_is_bounded():
    """A wrong filter must not walk Mailgun's whole history and burn the
    container's time budget — the run is meant to end and scale to zero."""
    endless = [FakeResp(200, {"items": [event(f"<m-{i}@mg>")],
                              "paging": {"next": f"https://api.mailgun.net/v3/mg/e?p={i}"}})
               for i in range(50)]
    session = FakeGetSession(endless)
    client(None, session=session).fetch_events()
    assert len(session.calls) <= 20


# ---------------------------------------------------------------- transport
def test_a_5xx_is_retried_then_raises():
    session = FakeGetSession([FakeResp(503, text="busy")] * 3)
    with pytest.raises(MailgunError):
        client(None, session=session).fetch_events()
    assert len(session.calls) == 3


def test_a_4xx_is_not_retried():
    session = FakeGetSession([FakeResp(401, text="unauthorized")])
    with pytest.raises(MailgunError):
        client(None, session=session).fetch_events()
    assert len(session.calls) == 1


def test_a_failure_raises_rather_than_returning_an_empty_result():
    """Silently returning [] would read as 'no engagement', which is a lie
    the pipeline would then write to HubSpot."""
    with pytest.raises(MailgunError):
        client(None, session=FakeGetSession([FakeResp(500)] * 3)).fetch_events()


def test_the_hop_is_audited():
    obs = RecordingObs()
    client([FakeResp(200, {"items": []})], obs=obs).fetch_events(correlation_token="t:r")
    assert obs.audits[0]["step"] == 8
    assert obs.audits[0]["direction"] == "outbound"
    assert obs.audits[0]["method"] == "GET"
    assert obs.processes[-1]["event"] == "mailgun_events_fetched"


def test_a_missing_domain_fails_loudly(monkeypatch):
    monkeypatch.delenv("MAILGUN_DOMAIN", raising=False)
    with pytest.raises(MailgunError):
        MailgunEventsClient(api_key="k", session=FakeGetSession(), obs=RecordingObs())


# --------------------------------------------------------------- tool surface
def test_the_named_tool_is_exposed():
    assert set(TOOLS) == {"fetch_event_status"}


def test_the_module_entry_point_uses_the_injected_client():
    session = FakeGetSession([FakeResp(200, {"items": [event()]})])
    events = fetch_event_status(correlation_token="trg-1:run-1",
                                client=client(None, session=session))
    assert len(events) == 1


def test_nothing_in_the_module_schedules_itself():
    """The whole objection on 2026-08-04 was to a scheduler keeping a
    container warm. Guard it structurally — against the parsed code, not the
    prose, so the docstrings explaining the rule cannot trip the check."""
    import ast
    import inspect

    import mailgun_tool as module

    tree = ast.parse(inspect.getsource(module))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"threading", "sched", "asyncio", "multiprocessing"}

    # No unbounded loop. `while url and pages < _MAX_PAGES` is bounded and fine;
    # `while True` is how a poller is spelled.
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            assert not (isinstance(node.test, ast.Constant) and node.test.value is True)


def test_an_os_level_failure_is_retried_not_crashed():
    """Same trap as the HubSpot client: a plain OSError is not a
    RequestException and must not escape the retry loop."""
    class ExplodingSession:
        def __init__(self):
            self.calls = 0

        def get(self, *a, **k):
            self.calls += 1
            raise OSError("TLS CA bundle unreadable")

    session = ExplodingSession()
    with pytest.raises(MailgunError):
        client(None, session=session).fetch_events()
    assert session.calls == 3
