package com.tongji.moderation;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ModerationAgentClientDecisionTest {

    @Test
    void waitingForHumanReviewMustNotApplyAgentRecommendation() {
        var decision = new ModerationAgentClient.ModerationAgentDecision(
                "task-1", "WAITING_REVIEW", "REJECT", "needs a human"
        );

        assertFalse(decision.pass());
        assertFalse(decision.reject());
    }

    @Test
    void onlyCompletedDecisionCanPublishOrReject() {
        assertTrue(new ModerationAgentClient.ModerationAgentDecision(
                "task-1", "COMPLETED", "PASS", ""
        ).pass());
        assertTrue(new ModerationAgentClient.ModerationAgentDecision(
                "task-2", "COMPLETED", "LIMIT", "limited"
        ).reject());
    }
}
