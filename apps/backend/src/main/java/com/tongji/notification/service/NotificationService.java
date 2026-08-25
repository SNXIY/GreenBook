package com.tongji.notification.service;

import com.tongji.notification.api.dto.NotificationPageResponse;

public interface NotificationService {
    void notifyReaction(String eventId, long actorId, String entityType, String entityId, String metric);

    void notifyCommentCreated(String eventId, long actorId, long postId, Long parentId, long postCreatorId);

    void notifyFollowCreated(String eventId, long actorId, long receiverId);

    /** 定时/立即发布成功后通知帖子作者（系统通知，actorId 固定为 0）。 */
    void notifyPostPublished(String eventId, long userId, long postId, Long scheduleId);

    NotificationPageResponse list(long receiverId, Long cursor, int size);

    long unreadCount(long receiverId);

    void markRead(long receiverId, java.util.List<String> ids);

    void markAllRead(long receiverId);
}
