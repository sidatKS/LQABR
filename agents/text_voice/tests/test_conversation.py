import conversation

CONTEXT = {"first_name": "Jane", "company": "Acme", "industry": "Mfg",
           "sender_name": "Sam"}


def url_for(step):
    return f"https://x.example/voice/qa?contact_id=1&step={step}"


def test_voicemail_twiml_contains_personalized_message():
    twiml = conversation.twiml_voicemail(CONTEXT)
    assert twiml.startswith("<?xml")
    assert "Jane" in twiml and "Acme" in twiml and "<Hangup/>" in twiml


def test_question_twiml_gathers_speech_with_action_url():
    twiml = conversation.twiml_question(1, CONTEXT, url_for(1))
    assert '<Gather input="speech"' in twiml
    assert "step=1" in twiml


def test_affirmative_detection():
    assert conversation.is_affirmative("Yes please")
    assert conversation.is_affirmative("yeah sure")
    assert not conversation.is_affirmative("no thanks")
    assert not conversation.is_affirmative(None)


def test_no_answer_ends_flow_not_engaged():
    twiml, result = conversation.next_action(1, "no", CONTEXT, url_for)
    assert result is not None and result.completed and not result.engaged
    assert "<Hangup/>" in twiml


def test_yes_advances_to_next_question():
    twiml, result = conversation.next_action(1, "yes", CONTEXT, url_for)
    assert result is None
    assert "step=2" in twiml


def test_yes_through_final_step_is_engaged():
    step = conversation.FINAL_STEP
    twiml, result = conversation.next_action(step, "absolutely", CONTEXT, url_for)
    assert result is not None and result.engaged
    assert "Wonderful" in twiml
