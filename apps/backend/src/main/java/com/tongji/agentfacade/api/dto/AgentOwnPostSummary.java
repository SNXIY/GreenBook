package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "我的帖子摘要")
public record AgentOwnPostSummary(
        @Schema(description = "帖子ID") String postId,
        @Schema(description = "标题") String title,
        @Schema(description = "摘要") String summary,
        @Schema(description = "状态") String status,
        @Schema(description = "可见性") String visible,
        @Schema(description = "内容来源") String contentOrigin,
        @Schema(description = "创建时间 (ISO-8601)") java.time.Instant createdAt,
        @Schema(description = "发布时间 (ISO-8601)") java.time.Instant publishedAt
) {}
