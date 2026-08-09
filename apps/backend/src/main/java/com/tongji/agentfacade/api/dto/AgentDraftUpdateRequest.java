package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

import java.time.Instant;

@Schema(description = "草稿更新请求")
public record AgentDraftUpdateRequest(
        @Schema(description = "标题") String title,
        @Schema(description = "正文 Markdown") String content,
        @Schema(description = "摘要") String summary,
        @Schema(description = "标签") java.util.List<String> tags,
        @Schema(description = "可见性") String visibility,
        @Schema(description = "期望的草稿版本（ISO-8601更新时间），用于乐观锁冲突检测。不传则跳过版本检查")
        Instant expectedVersion
) {}
