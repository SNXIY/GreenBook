package com.tongji.assistant.api.dto;

import java.time.Instant;

public record AssistantEngagementPost(
        String id,
        String title,
        String description,
        String authorId,
        String authorNickname,
        Instant publishTime,
        long commentCount
) {
}
