package com.tongji.notification.service.impl;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.comment.mapper.CommentMapper;
import com.tongji.comment.model.CommentRow;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.id.SnowflakeIdGenerator;
import com.tongji.notification.api.dto.NotificationPageResponse;
import com.tongji.notification.api.dto.NotificationResponse;
import com.tongji.notification.mapper.NotificationMapper;
import com.tongji.notification.model.Notification;
import com.tongji.notification.service.NotificationService;
import com.tongji.user.domain.User;
import com.tongji.user.mapper.UserMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Slf4j
@Service
@RequiredArgsConstructor
public class NotificationServiceImpl implements NotificationService {
    private static final Duration LIKE_AGG_WINDOW = Duration.ofMinutes(30);
    private static final Duration DEDUP_TTL = Duration.ofDays(3);
    private static final String UNREAD_KEY_PREFIX = "notify:unread:";
    private static final String DEDUP_KEY_PREFIX = "dedup:notify:";

    private final NotificationMapper notificationMapper;
    private final SnowflakeIdGenerator idGenerator;
    private final StringRedisTemplate redis;
    private final ObjectMapper objectMapper;
    private final UserMapper userMapper;
    private final KnowPostMapper knowPostMapper;
    private final CommentMapper commentMapper;

    @Override
    @Transactional
    public void notifyReaction(String eventId, long actorId, String entityType, String entityId, String metric) {
        if (!"like".equals(metric) && !"fav".equals(metric)) {
            return;
        }
        ReactionTarget target = resolveReactionTarget(entityType, entityId);
        if (target == null || target.receiverId() == actorId) {
            return;
        }
        String type = "like".equals(metric) ? "LIKE" : "FAVORITE";
        String title = "LIKE".equals(type) ? "有人点赞了你的内容" : "有人收藏了你的内容";
        String content = target.targetTitle() == null || target.targetTitle().isBlank()
                ? title
                : target.targetTitle();
        String extra = toJson(Map.of(
                "entityType", entityType,
                "entityId", entityId,
                "postId", target.postId(),
                "metric", metric
        ));
        saveNotification(eventId, target.receiverId(), actorId, type,
                target.targetType(), target.targetId(), target.aggregateType(), target.aggregateId(),
                title, content, extra, true);
    }

    @Override
    @Transactional
    public void notifyCommentCreated(String eventId, long actorId, long postId, Long parentId, long postCreatorId) {
        if (parentId == null) {
            if (postCreatorId == actorId) {
                return;
            }
            KnowPost post = knowPostMapper.findById(postId);
            saveNotification(eventId, postCreatorId, actorId, "COMMENT",
                    "post", String.valueOf(postId), "post", String.valueOf(postId),
                    "有人评论了你的内容", displayPostTitle(post, "你的内容有新评论"),
                    toJson(Map.of("postId", postId)), false);
            return;
        }

        CommentRow parent = commentMapper.findById(parentId);
        if (parent == null || parent.getUserId() == null || parent.getUserId() == actorId) {
            return;
        }
        saveNotification(eventId, parent.getUserId(), actorId, "REPLY",
                "comment", String.valueOf(parentId), "post", String.valueOf(postId),
                "有人回复了你的评论", trim(parent.getContent(), 80),
                toJson(Map.of("postId", postId, "parentId", parentId)), false);
    }

    @Override
    @Transactional
    public void notifyFollowCreated(String eventId, long actorId, long receiverId) {
        if (actorId == receiverId) {
            return;
        }
        saveNotification(eventId, receiverId, actorId, "FOLLOW",
                "user", String.valueOf(actorId), "user", String.valueOf(receiverId),
                "有人关注了你", "新的关注者", toJson(Map.of("actorId", actorId)), false);
    }

    @Override
    @Transactional(readOnly = true)
    public NotificationPageResponse list(long receiverId, Long cursor, int size) {
        int limit = Math.min(Math.max(size, 1), 50) + 1;
        List<Notification> rows = notificationMapper.listByReceiver(receiverId, cursor, limit);
        boolean hasMore = rows.size() == limit;
        if (hasMore) {
            rows = new ArrayList<>(rows.subList(0, limit - 1));
        }
        Map<Long, User> users = loadActors(rows);
        List<NotificationResponse> items = rows.stream()
                .map(row -> toResponse(row, users.get(row.getLatestActorId() == null ? row.getActorId() : row.getLatestActorId())))
                .toList();
        String nextCursor = hasMore && !rows.isEmpty() ? String.valueOf(rows.get(rows.size() - 1).getId()) : null;
        return new NotificationPageResponse(items, nextCursor, hasMore);
    }

    @Override
    public long unreadCount(long receiverId) {
        String key = unreadKey(receiverId);
        String cached = redis.opsForValue().get(key);
        if (cached != null) {
            try {
                return Long.parseLong(cached);
            } catch (NumberFormatException ignored) {
            }
        }
        long count = notificationMapper.countUnread(receiverId);
        redis.opsForValue().set(key, String.valueOf(count), Duration.ofHours(12));
        return count;
    }

    @Override
    @Transactional
    public void markRead(long receiverId, List<String> ids) {
        if (ids == null || ids.isEmpty()) {
            return;
        }
        List<Long> longIds = ids.stream()
                .map(this::parseLong)
                .filter(id -> id != null && id > 0)
                .distinct()
                .toList();
        if (longIds.isEmpty()) {
            return;
        }
        int updated = notificationMapper.markReadBatch(receiverId, longIds, Instant.now());
        decrementUnread(receiverId, updated);
    }

    @Override
    @Transactional
    public void markAllRead(long receiverId) {
        int updated = notificationMapper.markAllRead(receiverId, Instant.now());
        decrementUnread(receiverId, updated);
    }

    private void saveNotification(String eventId,
                                  long receiverId,
                                  long actorId,
                                  String type,
                                  String targetType,
                                  String targetId,
                                  String aggregateType,
                                  String aggregateId,
                                  String title,
                                  String content,
                                  String extraJson,
                                  boolean aggregate) {
        Instant now = Instant.now();
        if (notificationMapper.insertDedup(eventId, receiverId, now) == 0) {
            return;
        }
        redis.opsForValue().set(dedupKey(eventId, receiverId), "1", DEDUP_TTL);

        if (aggregate) {
            Notification existing = notificationMapper.findRecentAggregate(
                    receiverId, type, targetType, targetId, now.minus(LIKE_AGG_WINDOW));
            if (existing != null) {
                boolean wasRead = Boolean.TRUE.equals(existing.getReadStatus());
                String mergedContent = mergeContent(type, existing.getActorCount() == null ? 2 : existing.getActorCount() + 1);
                notificationMapper.updateAggregate(existing.getId(), eventId, actorId, mergedContent, extraJson, now);
                if (wasRead) {
                    incrementUnread(receiverId);
                }
                return;
            }
        }

        Notification notification = Notification.builder()
                .id(idGenerator.nextId())
                .eventId(eventId)
                .receiverId(receiverId)
                .actorId(actorId)
                .latestActorId(actorId)
                .type(type)
                .targetType(targetType)
                .targetId(targetId)
                .aggregateType(aggregateType)
                .aggregateId(aggregateId)
                .title(title)
                .content(content)
                .extraJson(extraJson)
                .actorCount(1)
                .readStatus(false)
                .createTime(now)
                .updateTime(now)
                .build();
        notificationMapper.insert(notification);
        incrementUnread(receiverId);
    }

    private ReactionTarget resolveReactionTarget(String entityType, String entityId) {
        Long id = parseLong(entityId);
        if (id == null) {
            return null;
        }
        if ("knowpost".equals(entityType)) {
            KnowPost post = knowPostMapper.findById(id);
            if (post == null || post.getCreatorId() == null) {
                return null;
            }
            return new ReactionTarget(post.getCreatorId(), "post", entityId, "post", entityId,
                    entityId, displayPostTitle(post, "你的内容收到新互动"));
        }
        if ("comment".equals(entityType)) {
            CommentRow comment = commentMapper.findById(id);
            if (comment == null || comment.getUserId() == null) {
                return null;
            }
            String root = comment.getRootId() == null ? entityId : String.valueOf(comment.getRootId());
            return new ReactionTarget(comment.getUserId(), "comment", entityId, "post", String.valueOf(comment.getPostId()),
                    String.valueOf(comment.getPostId()), trim(comment.getContent(), 80));
        }
        return null;
    }

    private Map<Long, User> loadActors(List<Notification> rows) {
        List<Long> ids = rows.stream()
                .map(row -> row.getLatestActorId() == null ? row.getActorId() : row.getLatestActorId())
                .filter(id -> id != null && id > 0)
                .distinct()
                .toList();
        Map<Long, User> result = new LinkedHashMap<>();
        if (ids.isEmpty()) {
            return result;
        }
        for (User user : userMapper.listByIds(ids)) {
            result.put(user.getId(), user);
        }
        return result;
    }

    private NotificationResponse toResponse(Notification row, User actor) {
        Long actorId = row.getActorId();
        Long latestActorId = row.getLatestActorId();
        return new NotificationResponse(
                String.valueOf(row.getId()),
                row.getType(),
                row.getTitle(),
                row.getContent(),
                row.getTargetType(),
                row.getTargetId(),
                row.getAggregateType(),
                row.getAggregateId(),
                actorId == null ? null : String.valueOf(actorId),
                latestActorId == null ? null : String.valueOf(latestActorId),
                actor == null ? null : actor.getNickname(),
                actor == null ? null : actor.getAvatar(),
                row.getActorCount() == null ? 1 : row.getActorCount(),
                Boolean.TRUE.equals(row.getReadStatus()),
                row.getCreateTime()
        );
    }

    private void incrementUnread(long receiverId) {
        redis.opsForValue().increment(unreadKey(receiverId));
        redis.expire(unreadKey(receiverId), Duration.ofHours(12));
    }

    private void decrementUnread(long receiverId, int delta) {
        if (delta <= 0) {
            return;
        }
        Long value = redis.opsForValue().decrement(unreadKey(receiverId), delta);
        if (value != null && value < 0) {
            redis.opsForValue().set(unreadKey(receiverId), "0", Duration.ofHours(12));
        }
    }

    private String unreadKey(long receiverId) {
        return UNREAD_KEY_PREFIX + receiverId;
    }

    private String dedupKey(String eventId, long receiverId) {
        return DEDUP_KEY_PREFIX + eventId + ":" + receiverId;
    }

    private String toJson(Map<String, ?> map) {
        try {
            return objectMapper.writeValueAsString(map);
        } catch (Exception ex) {
            return "{}";
        }
    }

    private String displayPostTitle(KnowPost post, String fallback) {
        if (post == null || post.getTitle() == null || post.getTitle().isBlank()) {
            return fallback;
        }
        return trim(post.getTitle(), 80);
    }

    private String mergeContent(String type, int count) {
        if ("LIKE".equals(type)) {
            return count + " 人点赞了你的内容";
        }
        if ("FAVORITE".equals(type)) {
            return count + " 人收藏了你的内容";
        }
        return count + " 条新互动";
    }

    private String trim(String value, int max) {
        if (value == null || value.isBlank()) {
            return "";
        }
        String normalized = value.trim();
        return normalized.length() <= max ? normalized : normalized.substring(0, max) + "...";
    }

    private Long parseLong(String value) {
        try {
            return value == null ? null : Long.parseLong(value);
        } catch (NumberFormatException ex) {
            return null;
        }
    }

    private record ReactionTarget(
            long receiverId,
            String targetType,
            String targetId,
            String aggregateType,
            String aggregateId,
            String postId,
            String targetTitle
    ) {
    }
}
