package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "结构化错误响应")
public record AgentErrorResponse(
        @Schema(description = "错误代码", example = "DRAFT_NOT_FOUND") String code,
        @Schema(description = "内部诊断消息") String message,
        @Schema(description = "用户可读消息") String userMessage,
        @Schema(description = "是否可重试") boolean retryable,
        @Schema(description = "请求是否已提交") boolean requestCommitted,
        @Schema(description = "追踪ID") String traceId
) {}
