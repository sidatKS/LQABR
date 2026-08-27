"""Phase 2 — the terse | normal | debug axis.

The acceptance list from §6 of claude/LOG_DESIGN_2026-08-26.md.

The point of debug is narrower than "more logging": all six truncations happen
BEFORE json.dumps, so a structured file has been storing damaged strings in
well-formed fields. Debug stops the values arriving pre-mangled. It does not
relax redaction, and it does not relax the line-width invariant.
"""

from __future__ import annotations

import json
import logging

import pytest

from research_core.obs import (ConsoleFormatter, Observability, _GLYPHS_UNICODE,
                               current_mode, preview, redact, set_mode,
                               summarize_args)

BIG = "N" * 4000
MULTILINE = "First line of the note.\nSecond line, which must survive.\n" * 30


@pytest.fixture(autouse=True)
def _normal_again():
    yield
    set_mode("normal")


class _Sink(logging.Handler):
    def __init__(self, width: int = 165) -> None:
        super().__init__()
        self.setFormatter(ConsoleFormatter(colour=False, glyphs=_GLYPHS_UNICODE,
                                           width=width))
        self.rendered: list = []
        self.raw: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.rendered.append(self.format(record))
        self.raw.append(record.getMessage())


def _render(width: int, **fields):
    logger = logging.getLogger(f"lqabr.test.mode.{width}.{len(fields)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Sink(width)
    logger.addHandler(sink)
    Observability(run_id="res-mode", logger=logger).process.emit("model_request", **fields)
    return sink


# --- the six welds, one test each -----------------------------------------

def test_debug_gives_back_the_argument_itself():
    set_mode("debug")
    assert summarize_args({"lead_context": BIG})["lead_context"] == BIG


def test_debug_preview_is_the_input_unchanged_newlines_and_all():
    set_mode("debug")
    out = preview(MULTILINE)
    assert out == MULTILINE, "no trim, no marker, no whitespace collapse"
    assert "\n" in out, "the collapse was rewriting the payload"


def test_debug_does_not_trim_a_long_value_in_redact():
    set_mode("debug")
    assert len(redact({"note": BIG})["note"]) == 4000


def test_the_three_modes_differ_only_in_how_much_arrives():
    lengths = {}
    for mode in ("terse", "normal", "debug"):
        set_mode(mode)
        lengths[mode] = (len(preview(MULTILINE)), len(redact({"note": BIG})["note"]))
    assert lengths["terse"][0] == 0, "terse prints no preview at all"
    assert 0 < lengths["normal"][0] < lengths["debug"][0]
    assert lengths["terse"][1] == lengths["normal"][1] < lengths["debug"][1]


def test_terse_reproduces_the_old_detail_off_shape():
    set_mode("terse")
    assert preview(MULTILINE) == ""
    assert summarize_args({"a": BIG}) == {"keys": ["a"]}


# --- the invariants debug must not relax ----------------------------------

@pytest.mark.parametrize("mode", ["terse", "normal", "debug"])
def test_a_credential_is_redacted_in_every_mode(mode):
    """`<redacted>` is a substitution, not a concatenation — "no string
    concatenation in debug" does not reach it."""
    set_mode(mode)
    clean = redact({"api_key": "sk-ant-secret", "authorization": "Bearer x",
                    "hubspot_token": "pat-na1-x"})
    assert clean["api_key"] == "<redacted>"
    assert clean["authorization"] == "<redacted>"
    assert clean["hubspot_token"] == "<redacted>"


@pytest.mark.parametrize("mode", ["terse", "normal", "debug"])
def test_a_token_count_is_not_a_token_in_any_mode(mode):
    set_mode(mode)
    clean = redact({"max_tokens": 2000, "input_tokens": 16743,
                    "output_tokens": 573})
    assert (clean["max_tokens"], clean["input_tokens"],
            clean["output_tokens"]) == (2000, 16743, 573)


@pytest.mark.parametrize("mode", ["terse", "normal", "debug"])
@pytest.mark.parametrize("width", [90, 120, 165])
def test_no_console_line_exceeds_the_width_in_any_mode(mode, width):
    set_mode(mode)
    sink = _render(width, model="claude-sonnet-4-6", max_tokens=2000,
                   params={"tool": "web_search", "max_uses": 5,
                           "domains": ["a.example", "b.example"]},
                   note_preview=preview(MULTILINE))
    for rendered in sink.rendered:
        for line in rendered.split("\n"):
            assert len(line) <= width, f"{len(line)} > {width}: {line!r}"


def test_debug_drops_no_field_and_cuts_no_value():
    set_mode("debug")
    sink = _render(90, model="claude-sonnet-4-6", max_tokens=2000,
                   params={"tool": "web_search", "max_uses": 5},
                   note_preview=preview("UNIQUE-TAIL-MARKER " * 30))
    body = " ".join(sink.rendered[-1].split())
    assert "model: claude-sonnet-4-6" in sink.rendered[-1]
    assert "params.tool: web_search" in sink.rendered[-1], "one key per line"
    assert body.count("UNIQUE-TAIL-MARKER") == 30, "every repetition survived"
    assert "…" not in sink.rendered[-1] and " ..." not in sink.rendered[-1]


def test_a_record_holding_a_newline_is_still_exactly_one_json_line():
    set_mode("debug")
    sink = _render(120, note_preview=preview(MULTILINE))
    assert len(sink.raw[-1].splitlines()) == 1
    assert "\\n" in sink.raw[-1], "the newline is escaped, not lost"
    assert "\n" in json.loads(sink.raw[-1])["note_preview"], "and survives the round trip"


# --- the deprecated knob still works, and says so -------------------------

def test_log_detail_zero_resolves_to_terse_and_is_flagged(monkeypatch):
    from research_core.settings import get_settings
    monkeypatch.setenv("LQABR_RESEARCH_LOG_DETAIL", "0")
    monkeypatch.delenv("LQABR_RESEARCH_LOG_MODE", raising=False)
    settings = get_settings(refresh=True)
    assert settings.log_mode == "terse"
    assert settings.log_detail_deprecated is True


def test_log_detail_one_resolves_to_normal(monkeypatch):
    from research_core.settings import get_settings
    monkeypatch.setenv("LQABR_RESEARCH_LOG_DETAIL", "1")
    monkeypatch.delenv("LQABR_RESEARCH_LOG_MODE", raising=False)
    settings = get_settings(refresh=True)
    assert settings.log_mode == "normal"
    assert settings.log_detail_deprecated is True


def test_the_mode_env_var_wins_over_the_deprecated_one(monkeypatch):
    from research_core.settings import get_settings
    monkeypatch.setenv("LQABR_RESEARCH_LOG_DETAIL", "0")
    monkeypatch.setenv("LQABR_RESEARCH_LOG_MODE", "debug")
    settings = get_settings(refresh=True)
    assert settings.log_mode == "debug"
    assert settings.log_detail_deprecated is False


def test_an_unknown_mode_falls_back_rather_than_raising(monkeypatch):
    from research_core.settings import get_settings
    monkeypatch.setenv("LQABR_RESEARCH_LOG_MODE", "loud")
    assert get_settings(refresh=True).log_mode == "normal"
    set_mode("loud")
    assert current_mode() == "normal", "a logging setting must not stop a run"


def test_health_reports_the_active_mode(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_MCP_STARTUP_CHECK", "off")
    import service_app
    payload = service_app._health_payload()
    assert payload["logging"]["mode"] in ("terse", "normal", "debug")
    assert "dir" in payload["logging"] and "files" in payload["logging"]


# --- debug withholds nothing about the model call -------------------------

class _Rows(logging.Handler):
    """Records the JSON records themselves, not their console rendering."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.rows.append(json.loads(record.getMessage()))


def _obs(name: str = "payload"):
    logger = logging.getLogger(f"lqabr.test.{name}.{id(object())}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Rows()
    logger.addHandler(sink)
    return Observability(run_id="res-payload", logger=logger), sink


def _stub_provider(obs):
    """The real AnthropicWebSearch with a stubbed transport — the emit under
    test is the production one, not a re-implementation."""
    import types
    from research_core.search.anthropic_search import AnthropicWebSearch
    from research_core.settings import get_settings

    class _Block:
        type = "text"
        text = "A grounded note."

    class _Msg:
        content = [_Block()]
        stop_reason = "end_turn"
        usage = types.SimpleNamespace(input_tokens=10, output_tokens=5)

    provider = AnthropicWebSearch(settings=get_settings(refresh=True), obs=obs)
    provider._client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=lambda **kw: _Msg()))
    return provider


SYSTEM = "SYSTEM PROMPT " * 100


def test_debug_logs_the_full_system_prompt_on_every_request(monkeypatch):
    """In normal mode the system prompt is deduped after its first use. In
    debug that dedup is wrong: a reader landing on the fifth request must not
    have to scroll back through the run to learn what was asked."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    set_mode("debug")
    obs, sink = _obs()
    provider = _stub_provider(obs)
    for i in range(3):
        provider.research(f"prompt {i}", system=SYSTEM)
    requests = [r for r in sink.rows if r["event"] == "model_request"]
    assert len(requests) == 3
    for record in requests:
        assert len(record["system_preview"]) == len(SYSTEM)
        assert len(record["prompt_preview"]) == record["prompt_chars"]


def test_debug_logs_the_exact_payload_handed_to_the_sdk(monkeypatch):
    """`sent_keys` names the keys; it does not say what was in them. In debug
    the payload itself is the record, so "what did we send" has one answer."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    set_mode("debug")
    obs, sink = _obs()
    _stub_provider(obs).research("prompt zero", system=SYSTEM)
    payload = [r for r in sink.rows if r["event"] == "model_request"][0]["payload"]
    assert payload["messages"] == [{"role": "user", "content": "prompt zero"}]
    assert payload["system"] == SYSTEM
    assert payload["tools"][0]["type"]
    assert payload["model"] and payload["max_tokens"]


@pytest.mark.parametrize("mode_name", ["terse", "normal"])
def test_outside_debug_the_payload_is_not_logged(monkeypatch, mode_name):
    """The payload carries the whole prompt twice over. It is a debug
    affordance, not a new default."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    set_mode(mode_name)
    obs, sink = _obs()
    provider = _stub_provider(obs)
    for i in range(2):
        provider.research(f"prompt {i}", system=SYSTEM)
    requests = [r for r in sink.rows if r["event"] == "model_request"]
    assert all(r["payload"] == {} for r in requests)
    assert requests[1]["system_preview"] == "", "still deduped outside debug"


def test_debug_spills_an_audit_hop_instead_of_one_giant_line():
    """The process branch already spilled in debug; the hop branch did not, so
    a write hop printed the whole 1,600-character note as ONE console line —
    complete, and unreadable. Same treatment, same width invariant."""
    set_mode("debug")
    logger = logging.getLogger("lqabr.test.hopspill")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = _Sink(165)
    logger.addHandler(sink)

    body = "X" * 4000
    Observability(run_id="res-hop", logger=logger).hop(
        service="mcp", endpoint="http://localhost:8080/mcp", status=200,
        duration_ms=1.0, attempt=2,
        params={"tool": "upsert", "authorization": "Bearer NEVER", "note": body})

    lines = [line for block in sink.rendered for line in block.split("\n")]
    assert max(len(line) for line in lines) <= 165, "the width invariant holds"
    assert sum(line.count("X") for line in lines) == 4000, "and nothing was dropped"
    assert any("attempt 2" in line for line in lines), "the retry is on the line"
    assert any("authorization: <redacted>" in line for line in lines)
    assert not any("Bearer NEVER" in line for line in lines)
