package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.NotBlank;

@Schema(description = "评论回复请求")
public record AgentCommentReplyRequest(
        @Schema(description = "帖子ID") @NotBlank String postId,
        @Schema(description = "父评论ID") @NotBlank String parentCommentId,
        @Schema(description = "回复内容") @NotBlank String content
) {}
