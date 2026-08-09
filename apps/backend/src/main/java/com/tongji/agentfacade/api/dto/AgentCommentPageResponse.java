package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "评论分页响应")
public record AgentCommentPageResponse(
        @Schema(description = "评论列表") java.util.List<AgentCommentResponse> items,
        @Schema(description = "下一页游标") String nextCursor,
        @Schema(description = "是否有更多") boolean hasMore
) {}
