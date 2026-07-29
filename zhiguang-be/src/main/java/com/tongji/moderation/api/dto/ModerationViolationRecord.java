package com.tongji.moderation.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.Instant;

public record ModerationViolationRecord(
        @JsonProperty("content_id") String contentId,
        @JsonProperty("risk_type") String riskType,
        String action,
        String reason,
        @JsonProperty("created_at") Instant createdAt
) {
}
