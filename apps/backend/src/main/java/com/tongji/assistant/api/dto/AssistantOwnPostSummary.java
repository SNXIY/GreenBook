package com.tongji.assistant.api.dto;

import java.time.Instant;

public record AssistantOwnPostSummary(
        String id,
        String title,
        String status,
        String visible,
        Instant createTime,
        Instant publishTime
) {
}
