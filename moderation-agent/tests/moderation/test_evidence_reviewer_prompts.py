from agents.moderation.prompts import EVIDENCE_REVIEWER_SYSTEM_PROMPT


def test_reviewer_prompt_separates_review_from_final_enforcement() -> None:
    prompt = " ".join(EVIDENCE_REVIEWER_SYSTEM_PROMPT.split())

    assert "not a final enforcement decision maker" in prompt
    assert "Never invent evidence" in prompt
    assert "Do not output a new PASS, REJECT, LIMIT" in prompt
    assert "smallest correction" in prompt
    assert "HUMAN_REVIEW" in prompt
    assert "chain-of-thought" in prompt
