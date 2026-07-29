package com.tongji.moderation.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record AdminModerationReviewRequest(
        @NotBlank
        @Pattern(regexp = "PASS|REJECT|LIMIT", message = "action 必须是 PASS、REJECT 或 LIMIT")
        String action,
        @Pattern(regexp = "NORMAL|ADVERTISING|ABUSE|PRIVACY", message = "riskType 取值无效")
        String riskType,
        @Size(max = 2000)
        String comment,
        Integer expectedVersion
) {
}
