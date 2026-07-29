package com.tongji.assistant.api.dto;

public record AssistantContributorInsight(
        String userId,
        String nickname,
        long commentCount
) {
}
