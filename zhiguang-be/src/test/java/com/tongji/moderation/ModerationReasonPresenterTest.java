package com.tongji.moderation;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class ModerationReasonPresenterTest {

    @Test
    void translatesLegacyPreflightDiagnostics() {
        String raw = "Preflight L0 resolved the item before LangGraph Agent reasoning. "
                + "Reasons: L0_ABUSE_HARD.";

        assertEquals(
                "内容包含高置信度的人身攻击、辱骂或威胁表达，请修改后重新提交",
                ModerationReasonPresenter.forUser(raw)
        );
    }

    @Test
    void preservesAlreadyUserFacingReason() {
        assertEquals(
                "内容含有攻击性表达，请修改",
                ModerationReasonPresenter.forUser("内容含有攻击性表达，请修改")
        );
    }
}
