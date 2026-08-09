package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

@Schema(description = "创建草稿请求")
public record AgentDraftCreateRequest(
        @Schema(description = "标题", example = "社区运营周报")
        @Size(max = 256) String title,

        @Schema(description = "正文 Markdown", example = "# 本周社区动态\n...")
        @NotBlank String content,

        @Schema(description = "摘要", example = "本周社区运营数据总览")
        @Size(max = 200) String summary,

        @Schema(description = "可见性", example = "public", allowableValues = {"public", "followers", "school", "private", "unlisted"})
        @Pattern(regexp = "public|followers|school|private|unlisted") String visibility
) {}
