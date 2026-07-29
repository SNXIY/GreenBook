package com.tongji.moderation.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.Instant;

public record ModerationCommunityContentRecord(
        @JsonProperty("content_id") String contentId,
        @JsonProperty("content_type") String contentType,
        @JsonProperty("author_id") String authorId,
        String content,
        String title,
        @JsonProperty("audit_status") String auditStatus,
        @JsonProperty("created_at") Instant createdAt
) {
}
