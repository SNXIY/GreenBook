package com.tongji.assistant.api.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Size;

import java.util.List;

public record AssistantCapabilityRequest(
        @NotBlank @Size(max = 64) String runId,
        @NotEmpty @Size(max = 8) List<@NotBlank @Size(max = 80) String> actions,
        @Size(max = 20) List<@NotBlank @Size(max = 128) String> resources,
        @Min(30) @Max(604800) int ttlSeconds,
        @Min(1) @Max(5) int maxUses
) {
}
