package com.tongji.notification.service;

import com.tongji.notification.api.dto.NotificationPageResponse;

public interface NotificationService {
    void notifyReaction(String eventId, long actorId, String entityType, String entityId, String metric);

    void notifyCommentCreated(String eventId, long actorId, long postId, Long parentId, long postCreatorId);

    void notifyFollowCreated(String eventId, long actorId, long receiverId);

    NotificationPageResponse list(long receiverId, Long cursor, int size);

    long unreadCount(long receiverId);

    void markRead(long receiverId, java.util.List<String> ids);

    void markAllRead(long receiverId);
}
