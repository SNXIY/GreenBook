package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "用户分析摘要")
public record UserAnalyticsSummaryResponse(
        @Schema(description = "总发布数") long totalPublished,
        @Schema(description = "总获赞数") long totalLikesReceived,
        @Schema(description = "总评论数") long totalComments,
        @Schema(description = "总收藏数") long totalFavorites,
        @Schema(description = "粉丝数") long followerCount,
        @Schema(description = "关注数") long followingCount
) {}
