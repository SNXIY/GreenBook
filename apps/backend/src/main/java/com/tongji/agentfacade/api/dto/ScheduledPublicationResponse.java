package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

import java.time.Instant;

@Schema(description = "定时发布响应")
public record ScheduledPublicationResponse(
        @Schema(description = "定时任务ID") String scheduleId,
        @Schema(description = "草稿ID") String draftId,
        @Schema(description = "计划发布时间 (ISO-8601)") Instant runAt,
        @Schema(description = "时区") String timezone,
        @Schema(description = "状态 (SCHEDULED/CANCELLED/PUBLISHED/FAILED)") String status,
        @Schema(description = "乐观锁版本") Integer version,
        @Schema(description = "发布后的帖子ID") String publishedPostId,
        @Schema(description = "失败代码") String failureCode,
        @Schema(description = "失败消息") String failureMessage,
        @Schema(description = "创建时间 (ISO-8601)") Instant createdAt,
        @Schema(description = "更新时间 (ISO-8601)") Instant updatedAt
) {}
