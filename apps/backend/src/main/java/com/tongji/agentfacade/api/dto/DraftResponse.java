package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "草稿响应")
public record DraftResponse(
        @Schema(description = "草稿ID") String draftId,
        @Schema(description = "所属用户ID") String ownerId,
        @Schema(description = "标题") String title,
        @Schema(description = "正文内容") String content,
        @Schema(description = "摘要") String summary,
        @Schema(description = "标签") java.util.List<String> tags,
        @Schema(description = "可见性") String visibility,
        @Schema(description = "版本") Integer version,
        @Schema(description = "状态") String status,
        @Schema(description = "内容来源") String contentOrigin,
        @Schema(description = "创建时间 (ISO-8601)") java.time.Instant createdAt,
        @Schema(description = "更新时间 (ISO-8601)") java.time.Instant updatedAt
) {}
