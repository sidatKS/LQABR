"""The model call — with a fake model, so the suite needs no key and no network."""

from __future__ import annotations

import json

import pytest

from schema import SummaryValidationError
from summarizer import build_instruction, build_user_message, summarize
from summary_core.settings import Settings
from summary_core.types import NormalizedDocument

GOOD = {"title": "Spring Boot 4", "topic": "Java", "summary": "It shipped.",
        "key_points": ["virtual threads"], "concepts": ["JVM"],
        "technologies": ["Spring Boot"], "takeaways": ["upgrade"], "industry": "Software"}


def fake_model(answers):
    """A completion() stand-in that returns queued answers and records calls."""
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        return {"choices": [{"message": {"content": answer}}]}

    completion.calls = calls
    return completion


@pytest.fixture
def document():
    return NormalizedDocument(source_kind="url", source_ref="https://example.com/post",
                              title="Spring Boot 4", text="Body text.")


@pytest.fixture
def settings():
    return Settings(model="anthropic/claude-sonnet-5", temperature=0.4)


def test_happy_path(document, settings):
    model = fake_model([json.dumps(GOOD)])
    result = summarize(document, settings=settings, completion=model)
    assert result.summary == "It shipped."
    assert result.model == "anthropic/claude-sonnet-5"
    assert result.source_ref == "https://example.com/post"


def test_the_model_gets_the_document_and_its_provenance(document, settings):
    model = fake_model([json.dumps(GOOD)])
    summarize(document, settings=settings, completion=model)
    messages = model.calls[0]["messages"]
    assert messages[0]["role"] == "system" and "Do not invent" in messages[0]["content"]
    assert "source_kind: url" in messages[1]["content"]
    assert "Body text." in messages[1]["content"]
    assert model.calls[0]["temperature"] == 0.4


def test_truncation_is_disclosed_to_the_model(settings):
    document = NormalizedDocument(source_kind="url", source_ref="r", text="t", truncated=True)
    assert "truncated" in build_user_message(document)


def test_a_bad_answer_is_retried_once_with_the_reason(document, settings):
    model = fake_model(["not json at all", json.dumps(GOOD)])
    result = summarize(document, settings=settings, completion=model)
    assert result.summary == "It shipped."
    assert len(model.calls) == 2
    correction = model.calls[1]["messages"][-1]["content"]
    assert "rejected" in correction and "ONLY the JSON" in correction


def test_two_bad_answers_fail_the_run_rather_than_inventing_one(document, settings):
    model = fake_model(["nope", "still nope"])
    with pytest.raises(SummaryValidationError, match="after 2 attempts"):
        summarize(document, settings=settings, completion=model)
    assert len(model.calls) == 2


def test_litellm_object_shape_is_understood(document, settings):
    class Message:
        content = json.dumps(GOOD)

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    assert summarize(document, settings=settings,
                     completion=lambda **kw: Response()).summary == "It shipped."


def test_instruction_names_every_field_the_parser_enforces():
    instruction = build_instruction()
    for field in ("summary", "key_points", "technologies", "industry"):
        assert f'"{field}"' in instruction
