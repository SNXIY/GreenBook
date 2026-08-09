package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "发布响应")
public record PublishResponse(
        @Schema(description = "帖子ID") String postId,
        @Schema(description = "状态") String status,
        @Schema(description = "是否已发布") boolean alreadyPublished,
        @Schema(description = "发布时间 (ISO-8601)") java.time.Instant publishedAt
) {}
