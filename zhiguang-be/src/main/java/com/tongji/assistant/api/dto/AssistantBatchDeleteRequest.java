package com.tongji.assistant.api.dto;

import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

import java.util.List;

public record AssistantBatchDeleteRequest(
        @NotEmpty
        @Size(max = 20)
        List<@Pattern(regexp = "^[1-9][0-9]{0,18}$") String> postIds
) {
}
