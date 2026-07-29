package com.tongji.assistant.api.dto;

import java.time.Instant;

public record AssistantCapabilityResponse(
        String token,
        String capabilityId,
        Instant expiresAt
) {
}
