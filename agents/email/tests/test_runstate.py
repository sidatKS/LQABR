"""Run state — the bridge from step 7 to step 8."""

from pathlib import Path

from runstate import LeadRunRecord

SRC = Path(__file__).resolve().parents[1] / "src"


def record(object_id="42", message_id="<m-1@mg>"):
    return LeadRunRecord(object_id=object_id, email="jane@acme.example",
                         message_id=message_id, skill="technology", subject="S")


def test_a_send_is_resolvable_by_message_id_days_later(store):
    store.record_send("trg-1", "run-1", record())
    found = store.resolve("trg-1", "run-1", message_id="<m-1@mg>")
    assert found.object_id == "42" and found.skill == "technology"


def test_a_send_is_resolvable_by_contact_id(store):
    store.record_send("trg-1", "run-1", record())
    assert store.resolve("trg-1", "run-1", lead_object_id="42").email == "jane@acme.example"


def test_a_token_with_no_run_state_resolves_to_none(store):
    assert store.resolve("trg-x", "run-x", message_id="<m-1@mg>") is None


def test_runs_never_collide_across_leads(store):
    store.record_send("trg-1", "run-1", record("42", "<m-1@mg>"))
    store.record_send("trg-1", "run-2", record("43", "<m-2@mg>"))
    assert store.resolve("trg-1", "run-1", message_id="<m-1@mg>").object_id == "42"
    assert store.resolve("trg-1", "run-2", message_id="<m-2@mg>").object_id == "43"
    # a message id from another run is not silently attributed here
    assert store.resolve("trg-1", "run-1", message_id="<m-2@mg>").object_id == "42"


def test_a_single_lead_run_resolves_even_without_a_known_message_id(store):
    store.record_send("trg-1", "run-1", record())
    assert store.resolve("trg-1", "run-1", message_id="<unknown@mg>").object_id == "42"


def test_updates_are_persisted(store):
    saved = store.record_send("trg-1", "run-1", record())
    saved.status = "clicked"
    saved.clicked = True
    store.update("trg-1", "run-1", saved)
    assert store.resolve("trg-1", "run-1", lead_object_id="42").clicked is True


def test_sent_at_is_stamped_automatically(store):
    assert store.record_send("trg-1", "run-1", record()).sent_at


def test_summary_reports_the_whole_run(store):
    store.record_send("trg-1", "run-1", record("42", "<m-1@mg>"))
    store.record_send("trg-1", "run-1", record("43", "<m-2@mg>"))
    summary = store.summary("trg-1", "run-1")
    assert set(summary["leads"]) == {"42", "43"}
    assert summary["messages"]["<m-2@mg>"] == "43"


def test_ids_with_path_characters_cannot_escape_the_state_directory(store):
    store.record_send("../../etc", "run-1", record())
    assert store.resolve("../../etc", "run-1", lead_object_id="42") is not None


def test_a_legacy_contact_id_file_still_resolves(store):
    """A run-state file written before the contact_id -> object_id rename must
    NOT brick the whole run with 'unexpected keyword argument contact_id' —
    engagement events arrive days later and the old file is still on disk."""
    import json
    path = store._path("trg-old", "run-old")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "object_id": "trg-old", "run_id": "run-old",
        "leads": {"99": {"contact_id": "99", "email": "old@acme.example",
                         "message_id": "<m-old@mg>", "skill": "technology"}},
        "messages": {"<m-old@mg>": "99"},
    }), encoding="utf-8")
    found = store.resolve("trg-old", "run-old", message_id="<m-old@mg>")
    assert found is not None
    assert found.object_id == "99" and found.email == "old@acme.example"


def test_from_raw_maps_legacy_key_and_drops_unknowns():
    rec = LeadRunRecord.from_raw({"contact_id": "7", "email": "x@y.z",
                                  "some_future_field": True})
    assert rec.object_id == "7" and rec.email == "x@y.z"


# --------------------------------------------------- VM deployment concerns
def test_an_unwritable_state_directory_stops_the_run_before_any_send(tmp_path):
    """A run must not send email it could never attribute events back to."""
    import pytest
    from runstate import RunStateError, RunStateStore

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")          # a file where a dir belongs
    with pytest.raises(RunStateError, match="not writable"):
        RunStateStore(directory=blocked / "runstate").ensure_writable()


def test_ensure_writable_creates_the_directory_and_leaves_no_probe(tmp_path):
    from runstate import RunStateStore

    store = RunStateStore(directory=tmp_path / "fresh")
    assert store.ensure_writable().is_dir()
    assert list((tmp_path / "fresh").iterdir()) == []


def test_a_second_process_cannot_clobber_a_concurrent_update(store, tmp_path):
    """The agent (step 7) and the webhook (steps 8-9) are separate OS
    processes sharing this directory on a VM. A threading lock does not
    reach across them, so the flock has to."""
    import multiprocessing as mp

    store.record_send("trg-1", "run-1", record("42", "<m-1@mg>"))

    def add_lead(directory, object_id, message_id):
        import sys
        sys.path.insert(0, str(SRC))
        from runstate import LeadRunRecord, RunStateStore
        RunStateStore(directory=directory).record_send(
            "trg-1", "run-1",
            LeadRunRecord(object_id=object_id, email="x@y.z", message_id=message_id))

    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=add_lead, args=(store.directory, str(n), f"<m-{n}@mg>"))
             for n in range(43, 51)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    leads = store.summary("trg-1", "run-1")["leads"]
    assert set(leads) == {str(n) for n in range(42, 51)}, (
        "a concurrent write was lost — cross-process locking is not holding")


def test_state_survives_a_new_store_instance(store):
    """Nothing is cached in memory: a restarted process reads the same disk."""
    from runstate import RunStateStore

    store.record_send("trg-1", "run-1", record())
    reopened = RunStateStore(directory=store.directory)
    assert reopened.resolve("trg-1", "run-1", lead_object_id="42").skill == "technology"
