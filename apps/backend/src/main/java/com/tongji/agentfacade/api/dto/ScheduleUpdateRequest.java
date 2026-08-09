package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.NotNull;

import java.time.Instant;

@Schema(description = "修改定时发布时间请求")
public record ScheduleUpdateRequest(
        @Schema(description = "新发布时间 (ISO-8601)", example = "2026-08-08T09:00:00Z")
        @NotNull Instant runAt,

        @Schema(description = "当前版本号（乐观锁）")
        @NotNull Integer version
) {}
