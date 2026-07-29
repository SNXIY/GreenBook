package com.tongji.moderation.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.Instant;

public record ModerationReportEvidence(
        @JsonProperty("report_type") String reportType,
        String reason,
        @JsonProperty("reporter_id") String reporterId,
        @JsonProperty("created_at") Instant createdAt
) {
}
