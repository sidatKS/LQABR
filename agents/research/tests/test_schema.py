"""The A2A envelope: both ids are read out of the gateway's metadata."""

from __future__ import annotations

from schema import A2AEnvelope


def _envelope(metadata):
    return A2AEnvelope(jsonrpc="2.0", id="1", method="message/send",
                       params={"message": {"role": "user", "parts": []},
                               "metadata": metadata})


def test_reads_object_id_and_blog_key_from_metadata():
    """summary_objectId is the blog key. A stray blog_published_at is ignored,
    not mistaken for one — it stopped being the key on 2026-08-24."""
    env = _envelope({"objectId": "533963448020",
                     "blog_published_at": "2026-08-27T09:30:00Z",
                     "summary_objectId": "329444635358", "run_id": "run-1"})
    target = env.target()
    assert target.objectId == "533963448020"
    assert target.summary_objectId == "329444635358"
    assert not hasattr(target, "blog_published_at")
    assert env.run_id() == "run-1"


def test_falls_back_to_the_top_level_object_id_mirror():
    env = A2AEnvelope(jsonrpc="2.0", method="message/send",
                      params={"metadata": {}}, objectId="123")
    assert env.target().objectId == "123"


def test_missing_ids_come_back_empty_not_none():
    env = _envelope({})
    target = env.target()
    assert target.objectId == "" and target.summary_objectId == ""
