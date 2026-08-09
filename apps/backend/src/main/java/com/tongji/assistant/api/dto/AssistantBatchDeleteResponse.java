package com.tongji.assistant.api.dto;

import java.util.List;

public record AssistantBatchDeleteResponse(
        List<String> postIds,
        int deletedCount,
        int alreadyDeletedCount,
        String status
) {
}
