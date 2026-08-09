package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "帖子上下文（Agent 引用用）")
public record AgentPostContext(
        @Schema(description = "帖子ID") String postId,
        @Schema(description = "标题") String title,
        @Schema(description = "摘要") String description,
        @Schema(description = "正文内容") String body,
        @Schema(description = "标签") java.util.List<String> tags,
        @Schema(description = "作者ID") String authorId,
        @Schema(description = "作者昵称") String authorNickname,
        @Schema(description = "发布时间 (ISO-8601)") java.time.Instant publishTime,
        @Schema(description = "内容来源") String contentOrigin
) {}
