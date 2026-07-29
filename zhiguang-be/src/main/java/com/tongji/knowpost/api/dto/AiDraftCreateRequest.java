package com.tongji.knowpost.api.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record AiDraftCreateRequest(
        @NotNull Long creatorId,
        @NotBlank @Size(max = 256) String title,
        @NotBlank String bodyMarkdown,
        String sourceTaskId,
        String contentSha256,
        String description
) {
}
