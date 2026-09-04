"""The OTLP sink — what replaced the three log files.

`research_logging_otel.py` is `research_logging.py` with the file handlers
taken out and one OTLP handler put in. These tests pin the four things that
can silently break that swap:

    1. the handler is on `lqabr.research`, NOT on the root logger — the
       logger does not propagate, so the usual recipe exports nothing while
       looking healthy;
    2. a record's fields survive as OTel ATTRIBUTES, flat and typed, because
       a dict handed to the SDK whole is dropped;
    3. a credential is still redacted on the way to the wire;
    4. nothing here can kill a run — no SDK, no collector, a broken handler:
       the agent falls back to console-only and says so.

Offline, like the rest of the suite. The export tests use the SDK's in-memory
exporter and skip when OpenTelemetry is not installed; every other test runs
either way.
"""

from __future__ import annotations

import json
import logging

import pytest

from research_core import research_logging_otel as otel
from research_core.research_logging_otel import (STREAMS, ResearchLoggingOtel,
                                                 configure_logging,
                                                 otel_attributes, sink_state)

sdk = pytest.importorskip  # used per-test, not at import time


def _fresh() -> logging.Logger:
    root = logging.getLogger("lqabr.research")
    root.handlers.clear()
    for stream in STREAMS:
        root.getChild(stream).handlers.clear()
    return root


@pytest.fixture(autouse=True)
def _clean_handlers(monkeypatch):
    # conftest's _clean_env turns LQABR_RESEARCH_OTLP_ENABLED off suite-wide,
    # so `TestClient(app)` boots never touch a real socket. This whole file
    # is ABOUT the OTLP path, so it turns that back on for itself — safely,
    # because every test here that reaches `_otlp_handler` mocks it (or
    # forces OTEL_MISSING), so nothing here ever opens a real connection
    # either.
    monkeypatch.setenv("LQABR_RESEARCH_OTLP_ENABLED", "1")
    _fresh()
    otel.shutdown_logging()
    yield
    _fresh()
    otel.shutdown_logging()


# ── 1. the attribute translation ────────────────────────────────────────────

def test_stream_becomes_log_group():
    attrs = otel_attributes({"stream": "audit", "run_id": "res-1",
                             "event": "outbound_call", "ts": 1.5})
    assert attrs["log_group"] == "audit"
    assert attrs["stream"] == "audit"
    assert attrs["run_id"] == "res-1"          # unprefixed: the search key
    assert attrs["event"] == "outbound_call"
    assert attrs["ts"] == 1.5


def test_agent_fields_are_namespaced_and_flat():
    attrs = otel_attributes({"stream": "audit", "event": "outbound_call",
                             "service": "anthropic",
                             "params": {"model": "sonnet",
                                        "tools": ["web_search", "note"]},
                             "input_tokens": 1200})
    assert attrs["lqabr.service"] == "anthropic"
    assert attrs["lqabr.params.model"] == "sonnet"        # flattened, not a dict
    assert attrs["lqabr.input_tokens"] == 1200            # stays an int
    assert attrs["lqabr.params.tools"] == ["web_search", "note"]
    assert not any(isinstance(v, dict) for v in attrs.values())


def test_lists_are_homogeneous_strings():
    # The SDK rejects a mixed sequence outright; one of these must not cost
    # the whole record its attributes.
    attrs = otel_attributes({"stream": "process", "event": "x",
                             "mixed": [1, "two", None]})
    assert attrs["lqabr.mixed"] == ["1", "two", "None"]


def test_deeper_than_the_flatten_limit_becomes_json():
    deep = {"a": {"b": {"c": {"d": {"e": "bottom"}}}}}
    attrs = otel_attributes({"stream": "process", "event": "x", "deep": deep})
    rendered = [v for k, v in attrs.items() if k.startswith("lqabr.deep")]
    assert rendered and all(isinstance(v, str) for v in rendered)


def test_empty_and_none_fields_are_dropped():
    attrs = otel_attributes({"stream": "process", "event": "x", "error": "",
                             "reason": None, "empty": {}, "none": []})
    assert not [k for k in attrs if k.startswith("lqabr.")]


def test_a_credential_never_reaches_an_attribute():
    attrs = otel_attributes({"stream": "audit", "event": "outbound_call",
                             "api_key": "sk-live-do-not-log",
                             "params": {"authorization": "Bearer nope",
                                        "secret_name": "lqabr-hubspot-token"}})
    assert "sk-live-do-not-log" not in json.dumps(attrs)
    assert "Bearer nope" not in json.dumps(attrs)
    # ...and the rule's other half: the credential's NAME still prints.
    assert attrs["lqabr.params.secret_name"] == "lqabr-hubspot-token"


# ── 2. degradation — a sink cannot kill a run ───────────────────────────────

def test_no_sdk_installed_falls_back_to_console(monkeypatch, capsys):
    monkeypatch.setattr(otel, "OTEL_MISSING",
                        "opentelemetry-sdk: No module named 'opentelemetry'")

    configure_logging("INFO")
    root = logging.getLogger("lqabr.research")

    assert sink_state()["exporter"] == "none"
    assert sink_state()["degraded"] == ["otlp:import"]
    # The console is still there, and nothing is exporting.
    assert any(getattr(h, "_lqabr_console", False) for h in root.handlers)
    assert not any(getattr(h, "_lqabr_otlp", False) for h in root.handlers)
    reported = capsys.readouterr().err
    assert "log_sink_unavailable" in reported         # named, not swallowed

    ResearchLoggingOtel(run_id="res-x").process.emit("still_running")   # no raise


def test_files_are_an_addon_not_a_replacement(monkeypatch, tmp_path):
    """OTLP was added ON TOP of the file sink, not instead of it. `log_dir`
    must keep doing exactly what it always did — three dated files, one per
    stream — with or without OTLP available."""
    monkeypatch.setattr(otel, "OTEL_MISSING", "opentelemetry-sdk: absent")  # OTLP off
    configure_logging("INFO", log_dir=str(tmp_path))
    ResearchLoggingOtel(run_id="res-file").process.emit("lead_read", company="Axiom")

    files = sink_state()["files"]
    assert set(files) == {"process", "audit", "system"}
    written = (tmp_path / files["process"].split("/")[-1]).read_text(encoding="utf-8")
    assert '"event": "lead_read"' in written and '"company": "Axiom"' in written
    # the other two streams' files exist too, just empty this run
    assert len(list(tmp_path.iterdir())) == 3


def test_log_file_legacy_knob_still_works(monkeypatch, capsys, tmp_path):
    """The deprecated single-file sink is untouched by adding OTLP."""
    monkeypatch.setattr(otel, "OTEL_MISSING", "opentelemetry-sdk: absent")
    target = tmp_path / "combined.log"
    configure_logging("INFO", log_file=str(target))
    ResearchLoggingOtel(run_id="res-legacy").audit.emit("outbound_call", service="x")

    seen = capsys.readouterr()
    assert "log_sink_legacy" in (seen.out + seen.err)
    assert target.exists()
    assert '"service": "x"' in target.read_text(encoding="utf-8")
    assert sink_state()["files"] == {"process": str(target), "audit": str(target),
                                     "system": str(target)}


def test_files_and_otlp_both_receive_the_same_record(monkeypatch, tmp_path):
    """The scenario the add-on exists for: one emit, every sink gets it."""
    pytest.importorskip("opentelemetry.sdk._logs")
    provider, exporter = _memory_provider()
    monkeypatch.setattr(otel, "_otlp_handler", lambda **kw: _built(provider))
    configure_logging("INFO", log_dir=str(tmp_path))

    ResearchLoggingOtel(run_id="res-both").process.emit("lead_read", company="Axiom")

    on_disk = (tmp_path / sink_state()["files"]["process"].split("/")[-1]).read_text()
    assert '"company": "Axiom"' in on_disk
    exported = [dict(r.log_record.attributes) for r in exporter.get_finished_logs()
               if dict(r.log_record.attributes).get("run_id") == "res-both"]
    assert exported and exported[0]["lqabr.company"] == "Axiom"


def test_filenames_have_no_date_in_them(tmp_path):
    """Fixed, undated per-stream files: `research_process.log`, not
    `research_process_2026-09-03.log`. A day boundary must not change which
    file a `tail -f` is watching."""
    configure_logging("INFO", log_dir=str(tmp_path))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["research_audit.log", "research_process.log",
                     "research_system.log"]


def test_a_stale_dated_file_from_the_old_scheme_is_left_alone(tmp_path):
    """There is no day-based sweep any more, and no size-based rotation
    either — the file is plain and uncapped. A leftover `_2020-01-01.log`
    from a previous build must not be touched, let alone deleted."""
    stale = tmp_path / "research_process_2020-01-01.log"
    stale.write_text("old", encoding="utf-8")
    configure_logging("INFO", log_dir=str(tmp_path), retention_days=1)
    assert stale.exists() and stale.read_text() == "old"


def test_file_grows_uncapped_no_rotation(tmp_path):
    """No file cap: the fixed-name file just grows. `max_bytes`/`backups`
    are accepted (for signature parity with the file-sink module) but do
    nothing — there is no rollover file to look for."""
    configure_logging("INFO", log_dir=str(tmp_path), max_bytes=200, backups=1)
    obs = ResearchLoggingOtel(run_id="res-grow")
    for _ in range(30):
        obs.process.emit("lead_read", note="x" * 40)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["research_audit.log", "research_process.log",
                     "research_system.log"]  # no "research_process.log.1" etc.
    path = tmp_path / "research_process.log"
    assert path.stat().st_size > 200  # past the old cap, never rotated


def test_export_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_OTLP_ENABLED", "0")
    configure_logging("INFO")
    assert sink_state()["exporter"] == "none"
    assert sink_state()["degraded"] == []             # off is not degraded


def test_a_foreign_handler_does_not_cost_us_the_console(monkeypatch, capsys):
    """`if not root.handlers` is the wrong question, and this is why.

    Something else — pytest's own capture, a host application, an SDK — puts a
    handler on `lqabr.research`. Asking whether the logger is EMPTY then skips
    our console handler for the whole process and leaves `propagate` True, so
    every record climbs to the root logger and prints a second time. We ask
    whether OUR handler is there instead.
    """
    monkeypatch.setattr(otel, "OTEL_MISSING", "opentelemetry-sdk: absent")
    root = logging.getLogger("lqabr.research")
    root.addHandler(logging.NullHandler())            # a stranger arrives first

    configure_logging("INFO", log_format="json")

    assert sum(getattr(h, "_lqabr_console", False) for h in root.handlers) == 1
    assert root.propagate is False
    ResearchLoggingOtel(run_id="res-1").process.emit("lead_read")
    assert "lead_read" in capsys.readouterr().out


def test_flush_and_shutdown_are_safe_with_no_provider():
    assert otel.flush_logging() is False
    otel.shutdown_logging()                            # no raise


# ── 3. the wiring — where the handler goes ──────────────────────────────────

def test_a_missing_exporter_package_is_named(monkeypatch, capsys):
    """grpc and http are separate packages. Missing one is only an error when
    it is the one the configured protocol asks for."""
    monkeypatch.setattr(otel, "EXPORTERS", {"grpc": None, "http": object()})
    monkeypatch.setenv("LQABR_RESEARCH_OTLP_PROTOCOL", "grpc")
    configure_logging("INFO")
    assert sink_state()["exporter"] == "none"
    assert "proto-grpc is not installed" in capsys.readouterr().err


def test_the_handler_resolved_is_the_non_deprecated_one():
    """The import line everyone writes is the one that raises.

    `from opentelemetry.instrumentation.logging import LoggingHandler` — what
    the SDK's deprecation notice points you at — is an ImportError: that
    package's `__init__` has `LoggingInstrumentor` and no handler. The handler
    lives one level down, in the `.handler` submodule. This pins that we end
    up on a real handler and prefer the non-deprecated one when it is there.
    """
    pytest.importorskip("opentelemetry.sdk._logs")
    handler_class = otel.LoggingHandler
    assert issubclass(handler_class, logging.Handler)
    try:
        import opentelemetry.instrumentation.logging.handler as current
    except ImportError:
        pytest.skip("opentelemetry-instrumentation-logging not installed")
    assert handler_class is current.LoggingHandler   # not the deprecated SDK one


def _memory_provider():
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (InMemoryLogExporter,
                                                SimpleLogRecordProcessor)
    from opentelemetry.sdk.resources import Resource
    exporter = InMemoryLogExporter()
    provider = LoggerProvider(resource=Resource.create({"service.name": "t"}))
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    return provider, exporter


def test_handler_lands_on_lqabr_research_not_on_root(monkeypatch):
    """The one mistake that exports nothing while looking healthy.

    `lqabr.research` sets `propagate = False`, so a handler on the Python root
    logger — what every OpenTelemetry quickstart tells you to add — is never
    reached by a single record this agent emits.
    """
    pytest.importorskip("opentelemetry.sdk._logs")
    provider, _ = _memory_provider()
    monkeypatch.setattr(otel, "_otlp_handler",
                        lambda **kw: _built(provider))

    configure_logging("INFO")
    root = logging.getLogger("lqabr.research")

    assert any(getattr(h, "_lqabr_otlp", False) for h in root.handlers)
    assert root.propagate is False
    assert not any(getattr(h, "_lqabr_otlp", False)
                   for h in logging.getLogger().handlers)


def _mine(exporter, run_id):
    """Only this run's records. The sink now reports ITSELF through the system
    stream — `log_sink_otlp` and friends export too, which is the point — so a
    test that counts records has to say whose."""
    return [dict(record.log_record.attributes)
            for record in exporter.get_finished_logs()
            if dict(record.log_record.attributes).get("run_id") == run_id]


def _events(exporter):
    return [dict(record.log_record.attributes).get("event")
            for record in exporter.get_finished_logs()]


def test_the_sink_reports_itself_on_the_system_stream(monkeypatch):
    """A boot note must not arrive at the collector unattributed.

    `root.warning(json.dumps(...))` — what the file-sink module does — carries
    no `lqabr_record`, so it exports with no `log_group` and no `run_id`: the
    one line that says where logs are going is the one line a query misses.
    """
    pytest.importorskip("opentelemetry.sdk._logs")
    provider, exporter = _memory_provider()
    monkeypatch.setattr(otel, "_otlp_handler", lambda **kw: _built(provider))
    configure_logging("INFO")

    boot = [dict(r.log_record.attributes) for r in exporter.get_finished_logs()
            if dict(r.log_record.attributes).get("event") == "log_sink_otlp"]
    assert boot and boot[0]["log_group"] == "system"
    assert boot[0]["run_id"].startswith("res-")


def _built(provider):
    from opentelemetry.sdk._logs import LoggingHandler
    handler = otel._make_stream_handler(LoggingHandler)(
        level=logging.NOTSET, logger_provider=provider)
    otel._SINK_STATE.update({"exporter": "otlp", "endpoint": "memory",
                             "protocol": "grpc", "service_name": "t",
                             "headers_from": ""})
    return handler


def test_configure_twice_exports_once(monkeypatch):
    pytest.importorskip("opentelemetry.sdk._logs")
    provider, exporter = _memory_provider()
    monkeypatch.setattr(otel, "_otlp_handler", lambda **kw: _built(provider))

    configure_logging("INFO")
    configure_logging("INFO")
    root = logging.getLogger("lqabr.research")
    assert sum(getattr(h, "_lqabr_otlp", False) for h in root.handlers) == 1

    ResearchLoggingOtel(run_id="res-1").process.emit("once")
    assert _events(exporter).count("once") == 1


# ── 4. end to end — a record, exported ──────────────────────────────────────

def test_every_stream_reaches_the_exporter(monkeypatch):
    pytest.importorskip("opentelemetry.sdk._logs")
    provider, exporter = _memory_provider()
    monkeypatch.setattr(otel, "_otlp_handler", lambda **kw: _built(provider))
    configure_logging("INFO")

    obs = ResearchLoggingOtel(run_id="res-abc")
    obs.process.emit("lead_read", objectId="533963448020")
    obs.audit.emit("outbound_call", service="hubspot", status=200)
    obs.system.emit("service_start", version="1.2.3")

    mine = _mine(exporter, "res-abc")
    assert sorted(a["log_group"] for a in mine) == ["audit", "process", "system"]
    assert all(a["run_id"] == "res-abc" for a in mine)


def test_a_hop_exports_its_params_and_its_cost(monkeypatch):
    pytest.importorskip("opentelemetry.sdk._logs")
    provider, exporter = _memory_provider()
    monkeypatch.setattr(otel, "_otlp_handler", lambda **kw: _built(provider))
    configure_logging("INFO")

    ResearchLoggingOtel(run_id="res-abc").hop(
        service="anthropic", endpoint="/v1/messages", status=200,
        duration_ms=812.0, params={"model": "sonnet", "api_key": "sk-live-xyz"},
        usage={"input_tokens": 1200, "output_tokens": 300})

    attrs = _mine(exporter, "res-abc")[0]
    assert attrs["log_group"] == "audit"
    assert attrs["lqabr.params.model"] == "sonnet"
    assert attrs["lqabr.input_tokens"] == 1200
    assert attrs["lqabr.status"] == 200
    assert "sk-live-xyz" not in json.dumps(dict(attrs))


def test_the_console_still_renders_the_same_record(monkeypatch, capsys):
    """The copy taken for the exporter must not disturb the original record."""
    pytest.importorskip("opentelemetry.sdk._logs")
    provider, exporter = _memory_provider()
    monkeypatch.setattr(otel, "_otlp_handler", lambda **kw: _built(provider))
    configure_logging("INFO", log_format="json")

    ResearchLoggingOtel(run_id="res-abc").process.emit("lead_read", company="Axiom")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()
             if line.startswith("{")]
    printed = [line for line in lines if line["event"] == "lead_read"]
    assert printed and printed[0]["company"] == "Axiom"
    assert _events(exporter).count("lead_read") == 1


def test_a_broken_exporter_does_not_stop_the_run(monkeypatch, capsys):
    pytest.importorskip("opentelemetry.sdk._logs")
    provider, _ = _memory_provider()
    monkeypatch.setattr(otel, "_otlp_handler", lambda **kw: _built(provider))
    configure_logging("INFO")

    root = logging.getLogger("lqabr.research")
    handler = [h for h in root.handlers if getattr(h, "_lqabr_otlp", False)][0]
    monkeypatch.setattr(type(handler).__mro__[1], "emit",
                        lambda self, record: (_ for _ in ()).throw(RuntimeError("gone")))

    ResearchLoggingOtel(run_id="res-abc").process.emit("lead_read")   # no raise
    assert "otlp:emit" in sink_state()["degraded"]
    assert "log_export_failed" in capsys.readouterr().err


# ── 5. the credential rule at configuration time ────────────────────────────

def test_headers_are_taken_by_name_and_only_the_name_is_reported(monkeypatch):
    monkeypatch.setenv("MY_COLLECTOR_HEADERS", "x-api-key=super-secret")
    monkeypatch.setenv("LQABR_RESEARCH_OTLP_HEADERS_ENV", "MY_COLLECTOR_HEADERS")
    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        otel._SINK_STATE.update({"exporter": "otlp", "endpoint": "e",
                                 "protocol": "grpc", "service_name": "s",
                                 "headers_from": kwargs["headers_from"]})
        return logging.NullHandler()

    monkeypatch.setattr(otel, "_otlp_handler", _capture)
    configure_logging("INFO")

    assert seen["headers"] == "x-api-key=super-secret"     # used
    assert sink_state()["headers_from"] == "MY_COLLECTOR_HEADERS"
    assert "super-secret" not in json.dumps(sink_state())  # never reported


def test_headers_string_parses(monkeypatch):
    assert otel._headers_dict("a=1,b=2") == {"a": "1", "b": "2"}
    assert otel._headers_dict("") == {}


def test_endpoint_and_protocol_come_from_env(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("LQABR_RESEARCH_OTLP_ENDPOINT", "http://collector:4318")
    seen = {}
    monkeypatch.setattr(otel, "_otlp_handler",
                        lambda **kw: (seen.update(kw), logging.NullHandler())[1])
    configure_logging("INFO")
    assert seen["protocol"] == "http"
    assert seen["endpoint"] == "http://collector:4318"


def test_configure_logging_takes_no_otlp_specific_parameters():
    """The signature must stay IDENTICAL to the file-sink module's — no
    otlp_endpoint / otlp_protocol / service_name / etc. Every real call site
    (agent.py, service_app.py) calls this with the same plain arguments it
    passes to research_logging.configure_logging; a Python-level override
    parameter that no caller ever populates is dead code, so all OTLP config
    is env-only."""
    import inspect
    from research_core import research_logging as file_sink
    file_params = list(inspect.signature(file_sink.configure_logging).parameters)
    otel_params = list(inspect.signature(configure_logging).parameters)
    assert otel_params == file_params


def test_standard_otel_env_is_honoured_as_a_fallback(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "sidecar:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "lqabr-dev-research")
    seen = {}
    monkeypatch.setattr(otel, "_otlp_handler",
                        lambda **kw: (seen.update(kw), logging.NullHandler())[1])
    configure_logging("INFO")
    assert seen["endpoint"] == "sidecar:4317"
    assert seen["service_name"] == "lqabr-dev-research"


# ── log ↔ trace correlation ─────────────────────────────────────────────────
#
# `current_trace_context()` is what links a log line to the span it happened
# inside. The SDK's LoggingHandler normally sets these itself from the ambient
# span, but this module attaches its OWN handler to `lqabr.research` (which
# does not propagate), and a record built by `_Stream.emit` carries no span
# context. Without the helper the two views stay unlinked - and that failure
# is SILENT: logs export fine, spans export fine, nothing joins them.


def test_no_span_in_flight_means_no_ids_and_no_exception():
    """Tracing is optional. No span is a normal state, not an error."""
    assert otel.current_trace_context() == {}


def test_a_span_in_flight_supplies_both_ids_correctly_formatted():
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("a-span") as span:
        ids = otel.current_trace_context()
        ctx = span.get_span_context()

    assert set(ids) == {"otelTraceID", "otelSpanID"}
    # Hex, zero-padded, the exact widths a backend joins on. A wrong width is
    # the failure that looks right in a log and matches nothing in a trace UI.
    assert ids["otelTraceID"] == format(ctx.trace_id, "032x")
    assert ids["otelSpanID"] == format(ctx.span_id, "016x")
    assert len(ids["otelTraceID"]) == 32 and len(ids["otelSpanID"]) == 16
    assert int(ids["otelTraceID"], 16) and int(ids["otelSpanID"], 16)


def test_the_ids_reach_the_record_the_handler_exports():
    """The seam that matters: emit() must stamp them on the copy it exports."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    seen = {}

    class _Base(logging.Handler):
        def emit(self, record):
            seen.update(vars(record))

    handler = otel._make_stream_handler(_Base)()
    record = logging.LogRecord("lqabr.research", logging.INFO, __file__, 1,
                               "{}", None, None)
    record.lqabr_record = {"stream": "process", "run_id": "res-1",
                           "event": "step_out"}

    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("outbound") as span:
        handler.emit(record)
        ctx = span.get_span_context()

    assert seen["otelTraceID"] == format(ctx.trace_id, "032x")
    assert seen["otelSpanID"] == format(ctx.span_id, "016x")
    # The ORIGINAL is untouched: the console and file sinks render a record
    # that never grew two fields they know nothing about.
    assert not hasattr(record, "otelTraceID")


def test_a_broken_trace_api_is_silent_not_fatal(monkeypatch):
    """A sink cannot kill a run - including this one."""
    class _Boom:
        def get_current_span(self):
            raise RuntimeError("tracer exploded")

    monkeypatch.setattr(otel, "_otel_trace", _Boom())
    assert otel.current_trace_context() == {}


def test_no_sdk_installed_is_also_silent(monkeypatch):
    monkeypatch.setattr(otel, "_otel_trace", None)
    assert otel.current_trace_context() == {}
