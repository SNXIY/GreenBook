from datetime import datetime, timedelta, timezone

from app.domain import AgentPlan
from app.worker import _parse_run_at
from app.token_vault import DelegatedTokenVault


def test_plan_contract_accepts_typed_tools() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "SCHEDULE_CREATE_AND_PUBLISH",
            "summary": "创作后定时发布",
            "response_guidance": "说明发布时间",
            "steps": [
                {
                    "tool": "creator.create_draft",
                    "label": "生成 Java 学习帖子",
                    "arguments": {"instruction": "如何学好 Java"},
                },
                {
                    "tool": "publication.schedule",
                    "label": "安排明早发布",
                    "arguments": {
                        "run_at": (
                            datetime.now(timezone.utc) + timedelta(days=1)
                        ).isoformat(),
                        "draft_id": "$last.draft_id",
                    },
                },
            ],
        }
    )
    assert plan.steps[1].tool == "publication.schedule"


def test_parse_run_at_normalizes_timezone() -> None:
    parsed = _parse_run_at("2026-07-29T08:00:00+08:00")
    assert parsed.tzinfo == timezone.utc
    assert parsed.hour == 0


def test_delegated_token_is_encrypted_at_rest() -> None:
    vault = DelegatedTokenVault("a-long-service-secret")
    ciphertext = vault.encrypt("jwt-value")
    assert "jwt-value" not in ciphertext
    assert vault.decrypt(ciphertext) == "jwt-value"
