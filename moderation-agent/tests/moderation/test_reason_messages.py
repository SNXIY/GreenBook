from moderation.schemas import ModerationAction, RiskType
from moderation.services.reason_messages import public_preflight_reason


def test_hard_abuse_reason_is_user_facing() -> None:
    reason = public_preflight_reason(
        reasons=["L0_ABUSE_HARD"],
        risk_type=RiskType.ABUSE,
        action=ModerationAction.REJECT,
    )

    assert reason == "内容包含高置信度的人身攻击、辱骂或威胁表达，请修改后重新提交。"
    assert "L0_" not in reason
    assert "LangGraph" not in reason


def test_unknown_privacy_reason_stays_actionable() -> None:
    reason = public_preflight_reason(
        reasons=["UNKNOWN_PRIVACY_RULE"],
        risk_type=RiskType.PRIVACY,
        action=ModerationAction.LIMIT,
    )

    assert reason == "内容可能包含敏感个人信息，请完成脱敏后重新提交。"
