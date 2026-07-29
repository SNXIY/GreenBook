package com.tongji.assistant.api.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;

public record AssistantPublishRequest(
        @NotNull Long creatorId,
        @NotBlank
        @Pattern(regexp = "^[0-9a-fA-F]{64}$")
        String expectedContentSha256
) {
}
