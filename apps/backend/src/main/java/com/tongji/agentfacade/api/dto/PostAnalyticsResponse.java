package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "帖子分析数据")
public record PostAnalyticsResponse(
        @Schema(description = "帖子ID") String postId,
        @Schema(description = "点赞数") long likeCount,
        @Schema(description = "评论数") long commentCount,
        @Schema(description = "收藏数") long favoriteCount,
        @Schema(description = "分享数") long shareCount,
        @Schema(description = "浏览量") long viewCount
) {}
