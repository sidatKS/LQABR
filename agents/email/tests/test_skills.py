"""Step 6 — skill selection and instruction-based construction."""

import pytest

import outreach

skills = outreach.skills


def _model(subject="Drafted subject", body="<p>Hi {employee_id}, drafted.</p>"):
    """A stand-in for the one model call per lead."""
    def model_fn(prompt_body, facts):
        assert isinstance(prompt_body, str) and prompt_body
        assert isinstance(facts, dict)
        return subject, body
    return model_fn


# ------------------------------------------------------------------ loading
def test_every_skill_parses_and_each_one_claims_an_industry():
    """There is no default skill: selection is by industry alone, so a skill
    claiming no industry could never be chosen."""
    loaded = skills.load_skills()
    assert loaded, "no skills discovered"
    assert "default" not in loaded, "the default fallback skill was removed"
    for skill in loaded.values():
        assert skill.instructions
        assert skill.industries, f"{skill.name} claims no industry — unreachable"
        assert skill.source_path.endswith(skills.SKILL_FILENAME)


def test_every_skill_carries_a_description_for_selection_review():
    for name, skill in skills.load_skills().items():
        assert skill.description, f"{name} has no description frontmatter"


def test_shared_rules_are_prepended_to_every_skill_and_come_first():
    for skill in skills.load_skills().values():
        body = skill.prompt_body()
        assert "Never invent" in body
        assert body.index("Never invent") < body.index("## Who you are writing to") \
            if "## Who you are writing to" in body else True


def test_no_skill_can_override_the_never_invent_rules():
    """The rules are a separate file, not copy-pasted into each skill, so
    they cannot drift or be dropped from one of them."""
    for skill in skills.load_skills().values():
        assert "never invent" not in skill.instructions.lower(), (
            f"{skill.name} restates the shared rules — they belong in "
            f"{skills.RULES_FILENAME} only")


# ---------------------------------------------------------------- selection
@pytest.mark.parametrize("industry,expected", [
    ("Software", "technology"),
    ("SaaS", "technology"),
    ("Banking", "financial_services"),
    ("Healthcare", "healthcare"),
    ("Manufacturing", "manufacturing"),
])
def test_industry_selects_its_skill(industry, expected):
    skill, reason = skills.select_skill(industry)
    assert skill.name == expected
    assert industry in reason


def test_case_and_punctuation_do_not_change_the_selection():
    assert skills.select_skill("IT Services")[0].name == "technology"
    assert skills.select_skill("  financial-services ")[0].name == "financial_services"


def test_missing_industry_raises_rather_than_drafting_without_a_skill():
    """The industry selects the skill, and there is no default. A lead with
    no industry is flagged unresolved by the caller, not sent generic copy."""
    with pytest.raises(skills.SkillError) as excinfo:
        skills.select_skill(None)
    assert "no industry" in str(excinfo.value)


def test_unknown_industry_raises_rather_than_guessing_a_near_match():
    with pytest.raises(skills.SkillError) as excinfo:
        skills.select_skill("Competitive Underwater Basket Weaving")
    message = str(excinfo.value)
    assert "no skill" in message
    assert "Competitive Underwater Basket Weaving" in message


# ------------------------------------------------------------------ context
def test_build_context_reads_cta_and_sender_from_config(monkeypatch):
    monkeypatch.setenv("LQABR_CTA_URL", "https://example.test/overview")
    monkeypatch.setenv("LQABR_SENDER_NAME", "Dana")
    ctx = skills.build_context({"employee_id": "E00002"})
    assert ctx["cta_url"] == "https://example.test/overview"
    assert ctx["sender_name"] == "Dana"


def test_the_model_never_receives_the_reserved_markers_as_values():
    ctx = skills.build_context({"employee_id": "E00002", "company": "Acme"},
                               cta_url="https://example.test/x", sender_name="Dana")
    facts = skills.lead_facts(ctx)
    assert facts == {"employee_id": "E00002", "company": "Acme"}
    for reserved in skills.RESERVED_SLOTS:
        assert reserved not in facts


def test_empty_profile_fields_are_not_offered_to_the_model():
    facts = skills.lead_facts({"employee_id": "E00002", "company": "", "job_title": None})
    assert facts == {"employee_id": "E00002"}


# -------------------------------------------------------- code-side finalise
def test_reserved_markers_are_substituted_after_the_model():
    ctx = skills.build_context({"employee_id": "E00002"},
                               cta_url="https://example.test/x", sender_name="Dana")
    subject, body = skills.finalise(
        "Hi", '<p>Hi {employee_id} — <a href="{cta_url}">overview</a></p><p>{sender_name}</p>', ctx)
    assert "https://example.test/x" in body
    assert "Dana" in body
    assert "{cta_url}" not in body and "{sender_name}" not in body


def test_a_model_written_unsubscribe_link_is_discarded():
    ctx = skills.build_context({"employee_id": "E00002"})
    _, body = skills.finalise(
        "Hi",
        '<p>Body.</p><p><a href="https://evil.test/optout">Unsubscribe here</a></p>',
        ctx)
    assert "evil.test" not in body
    assert body.count("%unsubscribe_url%") == 1


def test_exactly_one_compliant_footer_is_appended():
    ctx = skills.build_context({"employee_id": "E00002"})
    _, body = skills.finalise("Hi", "<p>Body.</p>", ctx)
    assert body.endswith(skills.UNSUBSCRIBE_FOOTER)
    assert body.count("%unsubscribe_url%") == 1


def test_finalise_survives_literal_braces_a_model_might_write():
    ctx = skills.build_context({"employee_id": "E00002"})
    _, body = skills.finalise("Hi", "<p>Use {} or {anything} safely.</p>", ctx)
    assert "{anything}" in body


# -------------------------------------------------------------------- render
def test_render_drafts_from_instructions_and_substitutes_real_fields():
    ctx = skills.build_context({"employee_id": "E00002", "company": "Acme"},
                               cta_url="https://example.test/x", sender_name="Dana")
    skill, _ = skills.select_skill("Software")
    subject, body, used_model = skills.render(skill, ctx, _model())
    assert used_model is True
    assert "E00002" in body
    assert subject == "Drafted subject"


def test_render_passes_the_shared_rules_to_the_model():
    seen = {}

    def model_fn(prompt_body, facts):
        seen["prompt"] = prompt_body
        return "S", "<p>B</p>"

    skill, _ = skills.select_skill("Software")
    skills.render(skill, skills.build_context({"employee_id": "E00002"}), model_fn)
    assert "Never invent" in seen["prompt"]
    assert "Outreach email — technology" in seen["prompt"]


def test_no_model_raises_rather_than_sending_an_unapproved_email():
    """There is no template to fall back on once content is model-written."""
    skill, _ = skills.select_skill("Software")
    with pytest.raises(skills.SkillError, match="needs a model"):
        skills.render(skill, skills.build_context({"employee_id": "E00002"}), None)


def test_an_unusable_model_draft_raises_rather_than_sending_nothing():
    skill, _ = skills.select_skill("Software")
    with pytest.raises(skills.SkillError, match="no usable subject/body"):
        skills.render(skill, skills.build_context({"employee_id": "E00002"}), _model("", ""))
