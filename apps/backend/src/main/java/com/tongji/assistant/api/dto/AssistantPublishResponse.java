package com.tongji.assistant.api.dto;

public record AssistantPublishResponse(
        String id,
        String status,
        boolean replayed
) {
}

