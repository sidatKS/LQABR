"""The A2A envelope: both ids are read out of the gateway's metadata."""

from __future__ import annotations

from schema import A2AEnvelope, ResearchRequest


def _envelope(metadata):
    return A2AEnvelope(jsonrpc="2.0", id="1", method="message/send",
                       params={"message": {"role": "user", "parts": []},
                               "metadata": metadata})


def test_reads_object_id_and_blog_key_from_metadata():
    env = _envelope({"object_id": "533963448020",
                     "blog_published_at": "2026-08-27T09:30:00Z",
                     "summary_ref_id": "329444635358", "run_id": "run-1"})
    target = env.target()
    assert target.object_id == "533963448020"
    assert target.blog_published_at == "2026-08-27T09:30:00Z"
    assert target.summary_ref_id == "329444635358"
    assert env.run_id() == "run-1"


def test_falls_back_to_the_top_level_object_id_mirror():
    env = A2AEnvelope(jsonrpc="2.0", method="message/send",
                      params={"metadata": {}}, object_id="123")
    assert env.target().object_id == "123"


def test_missing_ids_come_back_empty_not_none():
    env = _envelope({})
    target = env.target()
    assert target.object_id == "" and target.blog_published_at == ""


def test_request_accepts_flat_or_nested():
    flat = ResearchRequest(object_id="1", blog_published_at="t").resolved()
    assert flat.object_id == "1" and flat.blog_published_at == "t"
    nested = ResearchRequest(target={"object_id": "2",
                                     "blog_published_at": "u"}).resolved()
    assert nested.object_id == "2" and nested.blog_published_at == "u"
