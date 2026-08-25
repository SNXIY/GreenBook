package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "Structured error response")
public record AgentErrorResponse(
        @Schema(description = "Stable error code") String code,
        @Schema(description = "Technical diagnostic without a stack trace") String message,
        @Schema(description = "Safe user-facing message") String userMessage,
        @Schema(description = "Whether a bounded retry may be considered") boolean retryable,
        @Schema(description = "Whether the downstream request may have committed") boolean requestCommitted,
        @Schema(description = "Trace ID") String traceId,
        @Schema(description = "Invalid field, when applicable") String field,
        @Schema(description = "Contract maximum, when applicable") Integer maxLength,
        @Schema(description = "Observed length, when applicable") Integer actualLength,
        @Schema(description = "Execution ID, when available") String executionId
) {
    public AgentErrorResponse(
            String code,
            String message,
            String userMessage,
            boolean retryable,
            boolean requestCommitted,
            String traceId
    ) {
        this(code, message, userMessage, retryable, requestCommitted, traceId, null, null, null, null);
    }
}
