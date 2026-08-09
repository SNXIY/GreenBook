package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "评论响应")
public record AgentCommentResponse(
        @Schema(description = "评论ID") String id,
        @Schema(description = "帖子ID") String postId,
        @Schema(description = "父评论ID") String parentId,
        @Schema(description = "根评论ID") String rootId,
        @Schema(description = "评论者ID") String userId,
        @Schema(description = "评论者昵称") String authorNickname,
        @Schema(description = "评论者头像") String authorAvatar,
        @Schema(description = "评论内容") String content,
        @Schema(description = "是否置顶") boolean isTop,
        @Schema(description = "回复数") int replyCount,
        @Schema(description = "点赞数") long likeCount,
        @Schema(description = "是否AI生成") boolean assistant,
        @Schema(description = "创建时间 (ISO-8601)") java.time.Instant createdAt
) {}
