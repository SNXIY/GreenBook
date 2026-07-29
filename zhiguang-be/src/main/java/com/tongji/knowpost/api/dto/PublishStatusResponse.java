package com.tongji.knowpost.api.dto;

public record PublishStatusResponse(
        String id,
        String status,
        String moderationTaskId,
        String reason
) {
    public PublishStatusResponse(String id, String status) {
        this(id, status, null, null);
    }
}
