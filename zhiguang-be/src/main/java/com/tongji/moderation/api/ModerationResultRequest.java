package com.tongji.moderation.api;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record ModerationResultRequest(
        @JsonProperty("content_id") @Size(max = 256) String contentId,
        @NotBlank @Size(max = 32) String status,
        @JsonProperty("final_action") @Size(max = 32) String finalAction,
        @Size(max = 2000) String reason
) {
}
