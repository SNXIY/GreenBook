package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "搜索帖子项")
public record SearchPostItem(
        @Schema(description = "帖子ID") String postId,
        @Schema(description = "作者ID") String authorId,
        @Schema(description = "标题") String title,
        @Schema(description = "摘要") String summary,
        @Schema(description = "标签") java.util.List<String> tags,
        @Schema(description = "点赞数") Long likeCount,
        @Schema(description = "评论数") Long commentCount,
        @Schema(description = "收藏数") Long favoriteCount,
        @Schema(description = "发布时间 (ISO-8601)") java.time.Instant publishedAt,
        @Schema(description = "热度分数") Double hotScore
) {}
