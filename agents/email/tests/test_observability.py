"""The four log streams and the correlation token."""

import json

import pytest

import observability as obs


def test_bind_run_generates_a_run_id_and_keeps_the_trigger():
    ctx = obs.bind_run("trg-9")
    assert ctx.object_id == "trg-9"
    assert ctx.run_id
    assert ctx.correlation_token == f"trg-9:{ctx.run_id}"


def test_a_run_cannot_be_started_without_a_object_id():
    with pytest.raises(ValueError):
        obs.bind_run("")


def test_correlation_token_round_trips_through_mailgun():
    ctx = obs.bind_run("trg-9")
    back = obs.parse_correlation_token(ctx.correlation_token)
    assert back == ctx


@pytest.mark.parametrize("junk", ["", "no-colon", ":", "trg:", ":run"])
def test_malformed_tokens_resolve_to_none_rather_than_crashing(junk):
    assert obs.parse_correlation_token(junk) is None


def test_every_stream_is_keyed_by_trigger_and_run(run_ctx):
    for emit, kwargs in (
        (obs.process, {"event": "x"}),
        (obs.audit, {"direction": "outbound", "endpoint": "/x"}),
        (obs.model, {"model_name": "m"}),
    ):
        record = emit(run_ctx, **kwargs)
        assert record["object_id"] == "trg-1"
        assert record["run_id"] == "run-1"


def test_system_log_is_the_one_stream_that_works_with_no_run():
    record = obs.system(event="container_started")
    assert record["stream"] == "system_log"
    assert record["object_id"] is None


def test_the_bearer_value_is_never_written_to_audit_log(run_ctx):
    record = obs.audit(run_ctx, direction="outbound", endpoint="/crm/v3/objects/contacts",
                       bearer="pat-super-secret-value")
    serialised = json.dumps(record)
    assert "pat-super-secret-value" not in serialised
    assert record["bearer_fingerprint"] == obs.bearer_fingerprint("pat-super-secret-value")


def test_fingerprints_are_stable_and_distinguish_bearers():
    assert obs.bearer_fingerprint("a") == obs.bearer_fingerprint("a")
    assert obs.bearer_fingerprint("a") != obs.bearer_fingerprint("b")
    assert obs.bearer_fingerprint(None) == "none"


def test_credential_shaped_fields_are_redacted_anywhere_they_appear(run_ctx):
    record = obs.process(run_ctx, event="x", api_key="mg-key",
                         nested={"client_secret": "shh", "safe": "visible"})
    assert record["api_key"] == "***redacted***"
    assert record["nested"]["client_secret"] == "***redacted***"
    assert record["nested"]["safe"] == "visible"


def test_model_content_is_off_by_default_because_of_pii(run_ctx, monkeypatch):
    monkeypatch.delenv("LQABR_EMAIL_LOG_MODEL_CONTENT", raising=False)
    record = obs.model(run_ctx, model_name="gemini-2.0-flash",
                       input_tokens=10, output_tokens=20,
                       prompt="Dear Jane at Acme", completion="Hi Jane")
    assert "prompt" not in record and "completion" not in record
    assert record["content_logged"] is False
    assert record["input_tokens"] == 10 and record["output_tokens"] == 20


def test_model_content_is_written_only_when_explicitly_enabled(run_ctx, monkeypatch):
    monkeypatch.setenv("LQABR_EMAIL_LOG_MODEL_CONTENT", "1")
    record = obs.model(run_ctx, model_name="m", prompt="Dear Jane")
    assert record["prompt"] == "Dear Jane"
    assert record["content_logged"] is True


def test_unknown_stream_is_rejected(run_ctx):
    with pytest.raises(ValueError):
        obs.emit("business_log", run_ctx, event="x")


def test_audit_direction_must_be_inbound_or_outbound(run_ctx):
    with pytest.raises(ValueError):
        obs.audit(run_ctx, direction="sideways", endpoint="/x")


def test_streams_tee_to_disk_when_a_log_dir_is_configured(run_ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("LQABR_EMAIL_LOG_DIR", str(tmp_path))
    obs.process(run_ctx, event="run_started", step=3)
    lines = (tmp_path / "process_log.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[-1])["event"] == "run_started"
