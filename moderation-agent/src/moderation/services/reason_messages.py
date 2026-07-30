from moderation.schemas import ModerationAction, RiskType

_PREFLIGHT_MESSAGES = {
    "L0_ABUSE_HARD": "内容包含高置信度的人身攻击、辱骂或威胁表达，请修改后重新提交。",
    "L0_ADVERTISING_HARD": "内容包含联系方式引流或广告推广信息，请移除相关内容后重新提交。",
    "L0_PRIVACY_IDENTITY": "内容可能暴露身份证件等敏感身份信息，请删除或脱敏后重新提交。",
    "L0_PRIVACY_PHONE_CONTACT": "内容可能暴露联系方式或存在私下导流风险，请修改后重新提交。",
    "L1_ABUSE_ENFORCE": "内容可能包含人身攻击、仇恨、威胁或暴力表达，请修改后重新提交。",
    "L1_CLEAR_SAFE": "内容已通过自动安全检查。",
}


def public_preflight_reason(
    *,
    reasons: list[str],
    risk_type: RiskType,
    action: ModerationAction,
) -> str:
    """Return actionable copy for end users while reason codes remain in audit state."""
    for code in reasons:
        message = _PREFLIGHT_MESSAGES.get(code)
        if message:
            return message
    if action == ModerationAction.PASS:
        return "内容已通过自动安全检查。"
    if risk_type == RiskType.PRIVACY:
        return "内容可能包含敏感个人信息，请完成脱敏后重新提交。"
    if risk_type == RiskType.ADVERTISING:
        return "内容可能包含广告推广或站外引流信息，请修改后重新提交。"
    if risk_type == RiskType.ABUSE:
        return "内容可能包含不友善或攻击性表达，请修改后重新提交。"
    return "内容未通过自动安全检查，请修改后重新提交。"
