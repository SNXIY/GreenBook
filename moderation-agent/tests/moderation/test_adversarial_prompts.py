from agents.moderation.prompts import (
    ADVERSARIAL_JUDGE_PROMPT,
    RISK_INVESTIGATOR_PROMPT,
    SAFE_ADVOCATE_PROMPT,
)


def test_risk_prompt_requires_grounded_non_final_findings() -> None:
    assert "not to make the final enforcement decision" in RISK_INVESTIGATOR_PROMPT
    assert "Never invent a policy" in RISK_INVESTIGATOR_PROMPT
    assert "quote content evidence exactly" in RISK_INVESTIGATOR_PROMPT.lower()
    assert "author history and reports as auxiliary signals" in RISK_INVESTIGATOR_PROMPT


def test_safe_prompt_does_not_allow_unconditional_defense() -> None:
    assert "not to excuse clear violations" in SAFE_ADVOCATE_PROMPT
    assert "Acknowledge clear violation evidence" in SAFE_ADVOCATE_PROMPT
    assert "Do not invent context" in SAFE_ADVOCATE_PROMPT


def test_judge_prompt_prioritizes_evidence_and_fail_closed_review() -> None:
    prompt = " ".join(ADVERSARIAL_JUDGE_PROMPT.split())
    assert "rather than taking a majority vote" in prompt
    assert "Platform policy has priority" in prompt
    assert "REJECT requires a concrete retrieved policy" in prompt
    assert "Use HUMAN_REVIEW" in prompt
