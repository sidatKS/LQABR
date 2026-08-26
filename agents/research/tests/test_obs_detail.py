"""Can a person read a run off the log and see exactly what was sent?

The complaint these guard: "logs should be clear — the inputs for each step,
the output, where the model is called and what parameters we send". So each
test here is one half of that question, and the two standing rules still hold:
a credential never appears, and no line wraps.
"""

from __future__ import annotations

import json
import logging

import pytest

from composer import Composer
from conftest import FakeMCPClient, FakeSearch
from pipeline import run_research
from research_core.mcp.client import MCPClient
from research_core.mcp.hubspot import HubSpotMCP
from research_core.obs import (ConsoleFormatter, Observability, _GLYPHS_UNICODE,
                               preview, redact, set_detail, summarize_args)
from research_core.search.anthropic_search import AnthropicWebSearch
from research_core.settings import get_settings
from schema import ResearchTarget

LEAD = {"employee_id": "E1", "company_id": "C1", "decision_maker_flag": "Yes",
        "industry": "HEALTHCARE", "company": "Axiom Law", "first_name": "Mahesh"}
BLOG = {"found": True, "ticket_hs_id": "T1", "summary": {
    "blog_summary": "Governed AI needs citations and sign-off.",
    "blog_industry": "HEALTHCARE", "blog_published_at": "2026-08-27T09:30:00Z"}}
TARGET = ResearchTarget(objectId="533963448020", summary_objectId="329605630651")


class _Records(logging.Handler):
    """Keeps the structured record AND the line a terminal would show."""

    def __init__(self, width: int = 165) -> None:
        super().__init__()
        self._fmt = ConsoleFormatter(colour=False, glyphs=_GLYPHS_UNICODE, width=width)
        self.records = []
        self.lines = []

    def emit(self, record):
        data = getattr(record, "lqabr_record", None)
        if data is not None:
            self.records.append(data)
        self.lines.append(self._fmt.format(record))


def _obs(width: int = 165, name: str = "detail"):
    logger = logging.getLogger(f"lqabr.test.{name}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Records(width)
    logger.addHandler(sink)
    return Observability(run_id="res-test", logger=logger), sink


def _one(sink, event):
    found = [r for r in sink.records if r["event"] == event]
    assert found, f"no {event} was logged; saw {[r['event'] for r in sink.records]}"
    return found[-1]


# --- the step frame: what went in, what came out --------------------------

def test_a_step_pair_names_its_inputs_and_its_outputs():
    obs, sink = _obs()
    with obs.step("read_blog", objectId="330008697562",
                  tool="get_blog_summary") as step:
        step.ok(blog_industry="FINANCIAL_SERVICES", summary_chars=1261)

    opened, closed = sink.records
    assert opened["step"] == "read_blog" and opened["tool"] == "get_blog_summary"
    assert closed["status"] == "ok" and closed["duration_ms"] is not None
    assert closed["blog_industry"] == "FINANCIAL_SERVICES"

    assert "IN  read_blog" in sink.lines[0] and "objectId=330008697562" in sink.lines[0]
    assert "OUT read_blog" in sink.lines[1] and "summary_chars=1261" in sink.lines[1]


def test_a_failed_step_is_marked_and_keeps_its_reason():
    obs, sink = _obs(name="failstep")
    with obs.step("write_context") as step:
        step.failed("crm-error: the MCP rejected the write")
    line = sink.lines[-1]
    assert _GLYPHS_UNICODE["bad"] in line
    assert "crm-error: the MCP rejected the write" in line


def test_a_step_left_by_an_exception_still_closes_and_names_it():
    """The frame is the point: a step that opens can never be left open."""
    obs, sink = _obs(name="boom")
    with pytest.raises(ValueError):
        with obs.step("research", objectId="1"):
            raise ValueError("the provider exploded")

    closed = _one(sink, "step_out")
    assert closed["status"] == "failed"
    assert "the provider exploded" in closed["reason"]
    assert closed["duration_ms"] is not None


def test_every_step_of_a_run_is_framed_in_order():
    """The whole point: read the log top to bottom and you have the run."""
    obs, sink = _obs(name="pipeline")
    settings = get_settings(refresh=True)
    hubspot = HubSpotMCP(client=FakeMCPClient(
        {"get_lead_profile": LEAD, "get_blog_summary": BLOG,
         "upsert_lead_profile": {"status": "updated"}}), settings=settings, obs=obs)
    composer = Composer(provider=FakeSearch(), settings=settings, obs=obs)

    run_research(TARGET, settings=settings, obs=obs, hubspot=hubspot, composer=composer)

    opened = [r["step"] for r in sink.records if r["event"] == "step_in"]
    assert opened == ["read_lead", "read_blog", "research", "write_context"]
    closed = {r["step"]: r for r in sink.records if r["event"] == "step_out"}
    assert set(closed) == set(opened)
    assert all(r["duration_ms"] is not None for r in closed.values())
    assert closed["read_lead"]["company"] == "Axiom Law"
    assert closed["write_context"]["write_status"] == "written"


def test_a_step_that_fails_closes_with_the_reason_not_silence():
    obs, sink = _obs(name="pipefail")
    settings = get_settings(refresh=True)
    hubspot = HubSpotMCP(client=FakeMCPClient({"get_lead_profile": {"found": False}}),
                         settings=settings, obs=obs)
    composer = Composer(provider=FakeSearch(), settings=settings, obs=obs)

    run_research(TARGET, settings=settings, obs=obs, hubspot=hubspot, composer=composer)

    closed = _one(sink, "step_out")
    assert closed["step"] == "read_lead" and closed["status"] == "failed"
    assert closed["reason"]


# --- where the model is called, and with what -----------------------------

class _Messages:
    def create(self, **payload):
        self.payload = payload
        return {"content": [{"type": "text", "text": "A grounded note.",
                             "citations": [{"url": "https://example.com/a"}]}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12043, "output_tokens": 812,
                          "server_tool_use": {"web_search_requests": 3}}}


class _FakeAnthropic:
    def __init__(self):
        self.messages = _Messages()


def test_the_model_call_says_where_it_goes_and_what_it_sends():
    obs, sink = _obs(name="model")
    settings = get_settings(refresh=True)
    provider = AnthropicWebSearch(settings=settings, obs=obs,
                                  client=_FakeAnthropic(), api_key="test-only")

    provider.research("research Axiom Law", system="you are a research assistant")

    request = _one(sink, "model_request")
    assert request["model"] == "claude-sonnet-4-6"
    assert request["max_tokens"] == settings.max_tokens
    assert request["search_tool"].startswith("web_search")
    assert request["search_max_uses"] == settings.search_max_uses
    assert request["prompt_chars"] == len("research Axiom Law")
    assert "research Axiom Law" in request["prompt_preview"]
    assert "you are a research assistant" in request["system_preview"]

    call = _one(sink, "outbound_call")
    assert call["service"] == "anthropic" and call["endpoint"] == "messages.create"
    assert call["params"]["model"] == "claude-sonnet-4-6"
    assert call["params"]["max_tokens"] == settings.max_tokens

    reply = _one(sink, "model_response")
    assert reply["stop_reason"] == "end_turn"
    assert reply["input_tokens"] == 12043 and reply["output_tokens"] == 812
    assert reply["web_search_requests"] == 3
    assert "A grounded note." in reply["text_preview"]


def test_the_write_says_exactly_what_it_is_about_to_send():
    obs, sink = _obs(name="write")
    settings = get_settings(refresh=True)
    client = FakeMCPClient({"get_lead_profile": LEAD, "get_blog_summary": BLOG,
                            "upsert_lead_profile": {"status": "updated"}})
    hubspot = HubSpotMCP(client=client, settings=settings, obs=obs)
    composer = Composer(provider=FakeSearch(text="A" * 900), settings=settings, obs=obs)

    run_research(TARGET, settings=settings, obs=obs, hubspot=hubspot, composer=composer)

    opened = [r for r in sink.records
              if r["event"] == "step_in" and r["step"] == "write_context"][-1]
    assert opened["tool"] == settings.mcp_tool_write
    assert opened["property_name"] == settings.hubspot_context_property
    assert opened["chars"] == 900         # the note, as it will be written


class _Reply:
    """Just enough of a requests.Response for the MCP client."""

    def __init__(self, payload, status=200):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", "Mcp-Session-Id": "s1"}
        self.text = "" if payload is None else json.dumps(payload)


class _Session:
    """A stubbed transport, so the REAL client does the logging under test."""

    def __init__(self, result):
        self._result = result

    def request(self, method, url, headers=None, json=None, timeout=None):
        method_name = (json or {}).get("method")
        if method_name == "notifications/initialized":
            return _Reply(None, 202)
        if method_name == "tools/call":
            import json as _json
            return _Reply({"jsonrpc": "2.0", "id": 2, "result": {
                "content": [{"type": "text", "text": _json.dumps(self._result)}]}})
        return _Reply({"jsonrpc": "2.0", "id": 1, "result": {}})


def test_the_write_call_carries_the_note_as_a_summarised_argument():
    """The one call whose arguments matter most — logged before it is made."""
    obs, sink = _obs(name="mcpargs")
    settings = get_settings(refresh=True)
    client = MCPClient(settings, session=_Session({"status": "updated"}), obs=obs)

    client.call_tool(settings.mcp_tool_write,
                     {"employee_id": "E1", "company_id": "C1",
                      "decision_maker_flag": "Yes", "lead_context": "N" * 4000})

    sending = _one(sink, "mcp_tool_call")
    assert sending["tool"] == settings.mcp_tool_write
    assert sending["arguments"]["employee_id"] == "E1"
    # The note is summarised, never pasted whole into a log line.
    assert sending["arguments"]["lead_context"].startswith("[4000 chars] NNN")

    hop = [r for r in sink.records if r["event"] == "outbound_call"][-1]
    assert hop["params"]["tool"] == settings.mcp_tool_write
    assert _one(sink, "mcp_tool_result")["kind"] == "object"


# --- the two standing rules -----------------------------------------------

def test_a_token_count_is_not_a_token():
    """`max_tokens=<redacted>` is the opposite of useful."""
    clean = redact({"max_tokens": 2000, "input_tokens": 12043,
                    "output_tokens": 812, "api_key": "sk-ant-secret",
                    "hubspot_token": "pat-na1-secret"})
    assert clean["max_tokens"] == 2000
    assert clean["input_tokens"] == 12043 and clean["output_tokens"] == 812
    assert clean["api_key"] == "<redacted>" and clean["hubspot_token"] == "<redacted>"


def test_a_credential_nested_in_a_parameter_bag_is_still_redacted():
    """A parameter bag is a dict inside a field — the rule must reach into it."""
    clean = redact({"params": {"model": "claude-sonnet-4-6",
                               "authorization": "Bearer pat-na1-secret"},
                    "rows": [{"api_key": "sk-ant-abc123"}]})
    assert clean["params"]["model"] == "claude-sonnet-4-6"
    assert clean["params"]["authorization"] == "<redacted>"
    assert clean["rows"][0]["api_key"] == "<redacted>"


def test_the_boot_config_shows_which_credentials_it_will_use():
    """`secrets_source` and the two secret NAMES are the first fields you want
    when a key does not resolve. They used to print as <redacted>."""
    from research_core.settings import get_settings
    config = redact({"config": get_settings(refresh=True).redacted()})["config"]
    assert config["secrets_source"] == "env"
    assert config["model_token_secret"] == "lqabr-anthropic-api-key"
    assert config["hubspot_token_secret"] == "lqabr-hubspot-access-token"
    # ...while the token itself is still a value, and still hidden.
    assert config["mcp_auth_token"] in ("<redacted>", "")
    assert config["mcp_protected"] is False


def test_an_outbound_call_renders_what_it_sent():
    obs, sink = _obs(name="hop")
    obs.hop(service="mcp", endpoint="http://localhost:8091/mcp", status=200,
            duration_ms=412.0, params={"tool": "get_lead_profile",
                                       "objectId": "533970643697"})
    line = sink.lines[-1]
    assert "tool=get_lead_profile" in line and "objectId=533970643697" in line
    assert "200" in line and "412ms" in line


def test_a_long_argument_becomes_a_length_marked_head():
    summarised = summarize_args({"objectId": "1", "lead_context": "N" * 4000})
    assert summarised["objectId"] == "1"
    assert summarised["lead_context"].startswith("[4000 chars] NNN")


def test_a_preview_is_marked_when_it_is_cut():
    text = "word " * 200
    assert preview(text, 40).endswith("chars)")
    assert preview("short enough") == "short enough"
    assert preview("") == ""


def test_detail_off_gives_back_the_terser_shape():
    """LQABR_RESEARCH_LOG_DETAIL=0 — no previews, no parameter bags."""
    try:
        set_detail(False)
        obs, sink = _obs(name="terse")
        obs.hop(service="anthropic", endpoint="messages.create", status=200,
                params={"model": "claude-sonnet-4-6"})
        assert preview("a much longer piece of prose") == ""
        assert _one(sink, "outbound_call")["params"] == {}
        assert summarize_args({"lead_context": "N" * 4000}) == {"keys": ["lead_context"]}
    finally:
        set_detail(True)


def test_a_preview_still_arrives_on_a_line_that_is_already_full():
    """A payload preview sorts last; a long row of fields must not eat it."""
    obs, sink = _obs(width=90, name="fullline")
    obs.process.emit("model_request", model="claude-sonnet-4-6", max_tokens=2000,
                     search_tool="web_search_20250305", search_max_uses=5,
                     timeout_s=90, prompt_chars=1069, system_chars=1841,
                     endpoint="anthropic.messages.create",
                     prompt_preview="Research this lead and write the note.")
    out = sink.lines[-1]
    assert "Research this lead and write the note." in out
    assert "\n" in out, "it should continue below, not be dropped"


def test_no_line_exceeds_the_width_once_previews_are_on():
    for width in (90, 120, 165):
        obs, sink = _obs(width=width, name=f"width{width}")
        with obs.step("research", objectId="533970643697", company="Brex",
                      industry="FINANCIAL_SERVICES", max_tokens=2000,
                      model="anthropic/claude-sonnet-4-6",
                      search_max_uses=5, target_words=160) as step:
            step.ok(chars=3557, words=420, sources=25, note_preview="B" * 900)
        obs.hop(service="anthropic", endpoint="messages.create", status=200,
                duration_ms=20738.0,
                params={"model": "claude-sonnet-4-6", "max_tokens": 2000,
                        "tool": "web_search_20250305", "max_uses": 5,
                        "prompt_chars": 1069, "system_chars": 1841})
        for rendered in sink.lines:
            for line in rendered.split("\n"):
                assert len(line) <= width, f"{len(line)} > {width}: {line!r}"


LONG_TRANSPORT_ERROR = (
    "HTTPConnectionPool(host='127.0.0.1', port=9): Max retries exceeded with url: "
    "/mcp (Caused by NewConnectionError(\"HTTPConnection object: Failed to "
    "establish a new connection: [Errno 111] Connection refused\"))")


def test_a_transport_error_on_a_call_does_not_run_off_the_edge():
    """Found by running it: the error was appended without measuring the line."""
    for width in (90, 140, 165):
        obs, sink = _obs(width=width, name=f"hoperr{width}")
        obs.hop(service="mcp", endpoint="http://localhost:8091/mcp",
                error=LONG_TRANSPORT_ERROR, params={"method": "initialize"})
        obs.process.emit("campaign_lead_done", objectId="533970643697",
                         position=4, of=5, status="failed", chars=0,
                         error=LONG_TRANSPORT_ERROR)
        for rendered in sink.lines:
            for line in rendered.split("\n"):
                assert len(line) <= width, f"{len(line)} > {width}: {line!r}"


def test_a_finished_lead_is_followed_by_a_gap_on_the_console_only():
    """Five leads run as one wall of text; the eye needs a seam between them."""
    obs, sink = _obs(name="gap")
    obs.process.emit("campaign_lead_start", objectId="1", position=2, of=5)
    obs.process.emit("campaign_lead_done", objectId="1", position=2, of=5,
                     status="completed", chars=3557)
    start, done = sink.lines
    assert not start.endswith("\n"), "the gap belongs after the lead, not before"
    assert done.endswith("\n\n") and "(3 left)" in done
    # The record itself is untouched: the file stays one JSON object per line.
    assert "\n" not in json.dumps(sink.records[-1])


def test_the_system_prompt_is_previewed_once_not_once_per_lead():
    """It is a FILE — byte-identical every time. Three lines per lead of the
    same text is repetition, not observability."""
    obs, sink = _obs(name="sysonce")
    provider = AnthropicWebSearch(settings=get_settings(refresh=True), obs=obs,
                                  client=_FakeAnthropic(), api_key="test-only")

    provider.research("lead one", system="the same system prompt")
    provider.research("lead two", system="the same system prompt")
    provider.research("lead three", system="a DIFFERENT system prompt")

    previews = [r["system_preview"] for r in sink.records
                if r["event"] == "model_request"]
    assert previews == ["the same system prompt", "", "a DIFFERENT system prompt"]
    # The size is on every one of them, so nothing is actually hidden.
    assert all(r["system_chars"] for r in sink.records
               if r["event"] == "model_request")
