package com.tongji.assistant.api.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record AssistantCommentReplyRequest(
        @NotBlank String postId,
        @NotBlank String parentCommentId,
        @NotBlank @Size(max = 64) String assistantRunId,
        @NotBlank @Size(max = 1000) String content
) {
}
