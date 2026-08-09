package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

import java.time.Instant;

@Schema(description = "创建定时发布请求")
public record ScheduleCreateRequest(
        @Schema(description = "草稿ID", example = "340415383330754560")
        @NotBlank String draftId,

        @Schema(description = "计划发布时间 (ISO-8601)", example = "2026-08-07T10:00:00Z")
        @NotNull Instant runAt,

        @Schema(description = "时区", example = "Asia/Shanghai", defaultValue = "Asia/Shanghai")
        String timezone
) {}
