package com.tongji.moderation;

public final class ModerationReasonPresenter {
    private ModerationReasonPresenter() {
    }

    public static String forUser(String rawReason) {
        if (rawReason == null || rawReason.isBlank()) {
            return "内容未通过审核，请修改后重新提交";
        }
        if (rawReason.contains("L0_ABUSE_HARD")) {
            return "内容包含高置信度的人身攻击、辱骂或威胁表达，请修改后重新提交";
        }
        if (rawReason.contains("L0_ADVERTISING_HARD")) {
            return "内容包含联系方式引流或广告推广信息，请移除相关内容后重新提交";
        }
        if (rawReason.contains("L0_PRIVACY_IDENTITY")) {
            return "内容可能暴露身份证件等敏感身份信息，请删除或脱敏后重新提交";
        }
        if (rawReason.contains("L0_PRIVACY_PHONE_CONTACT")) {
            return "内容可能暴露联系方式或存在私下导流风险，请修改后重新提交";
        }
        if (rawReason.contains("L1_ABUSE_ENFORCE")) {
            return "内容可能包含人身攻击、仇恨、威胁或暴力表达，请修改后重新提交";
        }
        if (rawReason.startsWith("Preflight ")
                || rawReason.contains("LangGraph Agent reasoning")) {
            return "内容未通过自动安全检查，请修改后重新提交";
        }
        return rawReason;
    }
}
