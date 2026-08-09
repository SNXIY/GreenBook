package com.tongji.assistant.api.dto;

import java.time.Instant;
import java.util.List;

public record AssistantPostContext(
        String id,
        String title,
        String description,
        String bodyMarkdown,
        List<String> tags,
        String authorId,
        String authorNickname,
        Instant publishTime,
        String contentOrigin,
        String contentSha256
) {
}
