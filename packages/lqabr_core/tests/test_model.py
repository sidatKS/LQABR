from lqabr_core.model import build_model


def test_gemini_model_passes_through_unwrapped():
    result = build_model("gemini-2.0-flash")
    assert result == "gemini-2.0-flash"


def test_non_gemini_model_is_wrapped_in_litellm():
    result = build_model("anthropic/claude-sonnet-5")
    # Real or stubbed LiteLlm (see root conftest.py) both expose .model.
    assert result.model == "anthropic/claude-sonnet-5"
    assert type(result).__name__ == "LiteLlm"
