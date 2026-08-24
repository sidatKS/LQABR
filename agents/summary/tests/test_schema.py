"""The model contract: what we accept back, and what we refuse."""

from __future__ import annotations

import pytest

from schema import (
    SUMMARY_FIELDS,
    SummaryRequest,
    SummaryValidationError,
    extract_json,
    parse_summary,
)


class TestExtractJson:
    def test_bare_object(self):
        assert extract_json('{"summary": "x"}') == {"summary": "x"}

    def test_code_fence_is_tolerated(self):
        assert extract_json('```json\n{"summary": "x"}\n```') == {"summary": "x"}

    def test_prose_around_the_object_is_tolerated(self):
        assert extract_json('Sure! Here it is:\n{"summary": "x"}\nHope that helps.') == {"summary": "x"}

    @pytest.mark.parametrize("bad,match", [
        ("", "returned nothing"),
        ("I could not read that page.", "valid JSON"),
        ("[1, 2, 3]", "not a JSON object"),
        ('{"summary": ', "valid JSON"),
    ])
    def test_a_non_answer_is_refused_never_guessed(self, bad, match):
        with pytest.raises(SummaryValidationError, match=match):
            extract_json(bad)


class TestParseSummary:
    def test_full_response(self):
        raw = """{"title": "T", "topic": "Testing", "summary": "S.",
                  "key_points": ["a"], "concepts": ["c"], "technologies": ["pytest"],
                  "takeaways": ["t"], "industry": "Software"}"""
        result = parse_summary(raw, source_kind="url", source_ref="https://x", model="m")
        assert (result.title, result.topic, result.industry) == ("T", "Testing", "Software")
        assert result.key_points == ["a"] and result.technologies == ["pytest"]
        assert result.source_kind == "url" and result.model == "m"
        assert result.raw == raw, "the model's own words are kept for audit"

    def test_missing_summary_is_refused_by_name(self):
        with pytest.raises(SummaryValidationError, match="missing required field"):
            parse_summary('{"title": "T"}')

    def test_empty_summary_counts_as_missing(self):
        with pytest.raises(SummaryValidationError, match="missing required field"):
            parse_summary('{"summary": "   "}')

    def test_absent_optional_fields_stay_empty_never_invented(self):
        result = parse_summary('{"summary": "S."}')
        assert (result.title, result.industry) == ("", "")
        assert result.key_points == [] and result.takeaways == []

    def test_a_string_where_a_list_was_asked_for_is_accepted(self):
        """Models do this. Coercing is safe; failing the run over it is not."""
        assert parse_summary('{"summary": "S.", "key_points": "one thing"}').key_points == ["one thing"]

    def test_the_instruction_and_the_parser_share_one_field_list(self):
        assert "summary" in SUMMARY_FIELDS and "industry" in SUMMARY_FIELDS


class TestRequest:
    def test_bare_url_string(self):
        assert SummaryRequest(source="https://example.com/x").to_spec().kind == "url"

    def test_full_object(self):
        spec = SummaryRequest(source={"kind": "api", "endpoint": "https://svc/x",
                                      "method": "POST"}).to_spec()
        assert (spec.kind, spec.method) == ("api", "POST")

    def test_hubspot_target_is_optional(self):
        assert SummaryRequest(source="text here").hubspot is None
