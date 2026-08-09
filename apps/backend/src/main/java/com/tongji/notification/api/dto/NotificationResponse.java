package com.tongji.notification.api.dto;

import java.time.Instant;

public record NotificationResponse(
        String id,
        String type,
        String title,
        String content,
        String targetType,
        String targetId,
        String aggregateType,
        String aggregateId,
        String actorId,
        String latestActorId,
        String actorName,
        String actorAvatar,
        int actorCount,
        boolean read,
        Instant createTime
) {
}
