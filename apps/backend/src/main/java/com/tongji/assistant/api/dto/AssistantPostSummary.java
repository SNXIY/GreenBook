package com.tongji.assistant.api.dto;

import java.time.Instant;
import java.util.List;

public record AssistantPostSummary(
        String id,
        String title,
        String description,
        List<String> tags,
        String authorId,
        String authorNickname,
        Instant publishTime
) {
}

