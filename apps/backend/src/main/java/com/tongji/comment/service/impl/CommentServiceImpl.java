package com.tongji.comment.service.impl;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.comment.api.dto.CommentCreateRequest;
import com.tongji.comment.api.dto.CommentPageResponse;
import com.tongji.comment.api.dto.CommentResponse;
import com.tongji.comment.mapper.CommentMapper;
import com.tongji.comment.model.Comment;
import com.tongji.comment.model.CommentRow;
import com.tongji.comment.service.CommentService;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.counter.service.CounterService;
import com.tongji.knowpost.id.SnowflakeIdGenerator;
import com.tongji.relation.outbox.OutboxMapper;
import lombok.RequiredArgsConstructor;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Service
@RequiredArgsConstructor
public class CommentServiceImpl implements CommentService {
    private static final String ENTITY_TYPE = "comment";
    private static final String HOT_KEY_PREFIX = "comment:hot:";

    private final CommentMapper commentMapper;
    private final SnowflakeIdGenerator idGenerator;
    private final CounterService counterService;
    private final StringRedisTemplate redis;
    private final OutboxMapper outboxMapper;
    private final ObjectMapper objectMapper;

    @Override
    @Transactional
    public CommentResponse create(long userId, CommentCreateRequest request) {
        if (request.content() == null || request.content().trim().isEmpty()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "评论内容不能为空");
        }
        if (request.content().trim().length() > 1000) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "评论内容不能超过1000字");
        }
        //根据知文id查询发帖的用户id
        Long postCreatorId = commentMapper.findPostCreatorId(request.postId());
        if (postCreatorId == null) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "内容不存在");
        }

        Long parentId = request.parentId();//父评论id
        Long rootId = null; //子评论id
        if (parentId != null) { //如果父评论id不为null
            CommentRow parent = commentMapper.findById(parentId);
            if (parent == null || !"published".equals(parent.getStatus())) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "父评论不存在");
            }
            if (!request.postId().equals(parent.getPostId())) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "父评论不属于当前内容");
            }
            rootId = parent.getRootId() == null ? parent.getId() : parent.getRootId();
        }

        long id = idGenerator.nextId();
        Instant now = Instant.now();
        Comment comment = Comment.builder()
                .id(id)
                .postId(request.postId())
                .parentId(parentId)
                .rootId(rootId)
                .userId(userId)
                .content(request.content().trim())
                .status("published")
                .isTop(false)
                .replyCount(0)
                .createTime(now)
                .updateTime(now)
                .build();
        commentMapper.insert(comment);
        if (parentId != null) {
            commentMapper.incrementReplyCount(parentId);
            if (rootId != null && !rootId.equals(parentId)) {
                commentMapper.incrementReplyCount(rootId);
            }
        }
        writeOutbox("COMMENT_CREATED", request.postId(), Map.of(
                "commentId", String.valueOf(id),
                "postId", String.valueOf(request.postId()),
                "parentId", parentId == null ? "" : String.valueOf(parentId),
                "userId", String.valueOf(userId),
                "postCreatorId", String.valueOf(postCreatorId)
        ));

        CommentRow row = commentMapper.findById(id);
        refreshHotScore(row);
        if (parentId != null) {
            refreshHotRank(parentId);
            if (rootId != null && !rootId.equals(parentId)) {
                refreshHotRank(rootId);
            }
        }
        return toResponse(row, userId);
    }

    @Override
    @Transactional(readOnly = true)
    public CommentResponse get(long commentId, Long currentUserId) {
        CommentRow row = commentMapper.findById(commentId);
        if (row == null || !"published".equals(row.getStatus())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "评论不存在");
        }
        return toResponse(row, currentUserId);
    }

    @Override
    @Transactional(readOnly = true)
    public CommentPageResponse list(long postId, Long parentId, Long cursor, int size, Long currentUserId) {
        int limit = Math.min(Math.max(size, 1), 50) + 1;
        List<CommentRow> rows = parentId == null
                ? commentMapper.listTopLevel(postId, cursor, limit)
                : commentMapper.listReplies(parentId, cursor, limit);
        boolean hasMore = rows.size() == limit;
        if (hasMore) {
            rows = new ArrayList<>(rows.subList(0, limit - 1));
        }
        List<CommentResponse> items = rows.stream()
                .map(row -> toResponse(row, currentUserId))
                .toList();
        String nextCursor = hasMore && !rows.isEmpty() ? String.valueOf(rows.get(rows.size() - 1).getId()) : null;
        return new CommentPageResponse(items, nextCursor, hasMore);
    }

    @Override
    @Transactional(readOnly = true)
    public List<CommentResponse> hot(long postId, int size, Long currentUserId) {
        int limit = Math.min(Math.max(size, 1), 20);
        String key = hotKey(postId);
        var range = redis.opsForZSet().reverseRange(key, 0, limit - 1);
        List<String> ids = range == null ? List.of() : range.stream().toList();
        if (ids.isEmpty()) {
            List<CommentRow> fallback = commentMapper.listTopLevel(postId, null, limit);
            return fallback.stream()
                    .map(row -> toResponse(row, currentUserId))
                    .toList();
        }
        List<Long> commentIds = ids.stream().map(Long::valueOf).toList();
        Map<Long, CommentRow> byId = new LinkedHashMap<>();
        for (CommentRow row : commentMapper.listByIds(commentIds)) {
            byId.put(row.getId(), row);
        }
        return commentIds.stream()
                .map(byId::get)
                .filter(row -> row != null && row.getParentId() == null)
                .limit(limit)
                .map(row -> toResponse(row, currentUserId))
                .toList();
    }

    @Override
    @Transactional
    public void delete(long userId, long commentId) {
        CommentRow row = commentMapper.findById(commentId);
        if (row == null) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "评论不存在");
        }
        int updated = commentMapper.softDelete(commentId, userId);
        if (updated == 0) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "评论不存在或无权限删除");
        }
        if (row.getParentId() == null) {
            redis.opsForZSet().remove(hotKey(row.getPostId()), String.valueOf(commentId));
        } else {
            commentMapper.decrementReplyCount(row.getParentId());
            if (row.getRootId() != null && !row.getRootId().equals(row.getParentId())) {
                commentMapper.decrementReplyCount(row.getRootId());
            }
            refreshHotRank(row.getParentId());
            if (row.getRootId() != null && !row.getRootId().equals(row.getParentId())) {
                refreshHotRank(row.getRootId());
            }
        }
        writeOutbox("COMMENT_DELETED", row.getPostId(), Map.of(
                "commentId", String.valueOf(commentId),
                "postId", String.valueOf(row.getPostId()),
                "userId", String.valueOf(userId)
        ));
    }

    @Override
    @Transactional
    public void updateTop(long userId, long commentId, boolean top) {
        CommentRow row = commentMapper.findById(commentId);
        if (row == null) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "评论不存在");
        }
        int updated = commentMapper.updateTop(commentId, userId, top);
        if (updated == 0) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "仅内容作者可置顶一级评论");
        }
        row.setIsTop(top);
        refreshHotScore(row);
    }

    @Override
    public void refreshHotRank(long commentId) {
        CommentRow row = commentMapper.findById(commentId);
        refreshHotScore(row);
    }

    public void incrementHotLikeScore(long commentId, int delta) {
        CommentRow row = commentMapper.findById(commentId);
        if (row == null || !"published".equals(row.getStatus())) {
            return;
        }
        Long rankedCommentId = row.getParentId() == null ? row.getId() : row.getRootId();
        if (rankedCommentId == null) {
            return;
        }
        CommentRow ranked = commentMapper.findById(rankedCommentId);
        if (ranked == null || !"published".equals(ranked.getStatus())) {
            return;
        }
        String key = hotKey(ranked.getPostId());
        String member = String.valueOf(ranked.getId());
        Double currentScore = redis.opsForZSet().score(key, member);
        if (currentScore == null) {
            refreshHotScore(ranked);
            return;
        }
        redis.opsForZSet().incrementScore(key, member, delta * 10D);
    }

    private CommentResponse toResponse(CommentRow row, Long currentUserId) {
        Map<String, Long> counts = counterService.getCounts(ENTITY_TYPE, String.valueOf(row.getId()), List.of("like"));
        long likeCount = counts.getOrDefault("like", 0L);
        boolean liked = currentUserId != null && counterService.isLiked(ENTITY_TYPE, String.valueOf(row.getId()), currentUserId);
        return new CommentResponse(
                String.valueOf(row.getId()),
                String.valueOf(row.getPostId()),
                row.getParentId() == null ? null : String.valueOf(row.getParentId()),
                row.getRootId() == null ? null : String.valueOf(row.getRootId()),
                String.valueOf(row.getUserId()),
                row.getAuthorNickname(),
                row.getAuthorAvatar(),
                row.getContent(),
                Boolean.TRUE.equals(row.getIsTop()),
                row.getReplyCount() == null ? 0 : row.getReplyCount(),
                likeCount,
                liked,
                Boolean.TRUE.equals(row.getAssistant()),
                row.getAssistantRunId(),
                row.getCreateTime()
        );
    }

    private void refreshHotScore(CommentRow row) {
        if (row == null || row.getParentId() != null || !"published".equals(row.getStatus())) {
            return;
        }
        redis.opsForZSet().add(hotKey(row.getPostId()), String.valueOf(row.getId()), hotScore(row));
    }

    private double hotScore(CommentRow row) {
        long likeCount = counterService.getCounts(ENTITY_TYPE, String.valueOf(row.getId()), List.of("like"))
                .getOrDefault("like", 0L);
        long replyCount = row.getReplyCount() == null ? 0L : row.getReplyCount();
        long ageHours = Math.max(1L, (System.currentTimeMillis() - row.getCreateTime().toEpochMilli()) / 3_600_000L);
        double topBoost = Boolean.TRUE.equals(row.getIsTop()) ? 1_000_000D : 0D;
        return topBoost + likeCount * 10D + replyCount * 3D - Math.log(ageHours + 1D);
    }

    private String hotKey(long postId) {
        return HOT_KEY_PREFIX + postId;
    }

    private void writeOutbox(String type, long postId, Map<String, String> payload) {
        try {
            long eventId = idGenerator.nextId();
            outboxMapper.insert(eventId, "comment", postId, type, objectMapper.writeValueAsString(payload));
        } catch (Exception ignored) {
        }
    }
}
