package com.tongji.assistant.api.dto;

import java.time.Instant;
import java.util.List;

public record AssistantEngagementAnalytics(
        String topic,
        Instant periodStart,
        Instant periodEnd,
        long publishedPostCount,
        long commentCount,
        long activeCreatorCount,
        long interactingUserCount,
        List<AssistantEngagementPost> topPosts,
        List<AssistantContributorInsight> topContributors,
        List<String> availableSignals,
        List<String> limitations
) {
}
