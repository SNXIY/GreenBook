package com.tongji.knowpost.api.dto;

import java.time.Instant;

public record PostTaskItemResponse(
        String id,
        String title,
        String status,
        String contentOrigin,
        Instant createdAt,
        Instant updatedAt,
        Instant publishedAt
) {
}
