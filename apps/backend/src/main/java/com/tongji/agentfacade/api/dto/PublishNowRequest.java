package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.NotBlank;

@Schema(description = "立即发布请求")
public record PublishNowRequest(
        @Schema(description = "草稿ID", example = "340415383330754560")
        @NotBlank String draftId
) {}
