package com.tongji.assistant.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.assistant.api.dto.AssistantPostContext;
import com.tongji.assistant.api.dto.AssistantPostSummary;
import com.tongji.assistant.api.dto.AssistantEngagementAnalytics;
import com.tongji.assistant.api.dto.AssistantEngagementPost;
import com.tongji.assistant.api.dto.AssistantContributorInsight;
import com.tongji.assistant.api.dto.AssistantPublishResponse;
import com.tongji.assistant.api.dto.AssistantCommentReplyRequest;
import com.tongji.assistant.api.dto.AssistantOwnPostSummary;
import com.tongji.assistant.api.dto.AssistantBatchDeleteResponse;
import com.tongji.assistant.mapper.AssistantCommentProvenanceMapper;
import com.tongji.comment.api.dto.CommentCreateRequest;
import com.tongji.comment.api.dto.CommentResponse;
import com.tongji.comment.service.CommentService;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.storage.OssStorageService;
import com.tongji.user.mapper.UserMapper;
import com.tongji.user.domain.User;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;
import java.time.Instant;
import java.time.temporal.ChronoUnit;

@Service
@RequiredArgsConstructor
public class AssistantToolService {
    private final KnowPostMapper knowPostMapper;
    private final KnowPostService knowPostService;
    private final OssStorageService storageService;
    private final ObjectMapper objectMapper;
    private final CommentService commentService;
    private final AssistantCommentProvenanceMapper provenanceMapper;
    private final UserMapper userMapper;

    @Transactional(readOnly = true)
    public List<AssistantPostSummary> search(String query, int limit) {
        String normalized = query == null ? "" : query.trim();
        if (normalized.isEmpty()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "搜索关键词不能为空");
        }
        if (normalized.length() > 100) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "搜索关键词不能超过100字");
        }
        int boundedLimit = Math.min(Math.max(limit, 1), 10);
        return knowPostMapper.searchPublicForAssistant(normalized, boundedLimit).stream()
                .map(row -> new AssistantPostSummary(
                        String.valueOf(row.getId()),
                        row.getTitle(),
                        row.getDescription(),
                        parseStringList(row.getTags()),
                        String.valueOf(row.getCreatorId()),
                        row.getAuthorNickname(),
                        row.getPublishTime()
                ))
                .toList();
    }

    @Transactional(readOnly = true)
    public AssistantPostContext getPost(long postId) {
        KnowPostDetailRow row = knowPostMapper.findDetailById(postId);
        if (row == null || !"published".equals(row.getStatus())
                || !"public".equals(row.getVisible())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "帖子不存在或不可公开读取");
        }
        String body = storageService.readTextObject(row.getContentObjectKey(), 512 * 1024);
        return new AssistantPostContext(
                String.valueOf(row.getId()),
                row.getTitle(),
                row.getDescription(),
                body,
                parseStringList(row.getTags()),
                String.valueOf(row.getCreatorId()),
                row.getAuthorNickname(),
                row.getPublishTime(),
                row.getContentOrigin(),
                row.getContentSha256()
        );
    }

    @Transactional(readOnly = true)
    public AssistantPostContext getOwnDraft(long postId, long creatorId) {
        KnowPostDetailRow row = knowPostMapper.findDetailById(postId);
        if (row == null || row.getCreatorId() == null
                || row.getCreatorId() != creatorId
                || !"draft".equals(row.getStatus())
                || !"AI_ASSISTED".equalsIgnoreCase(row.getContentOrigin())) {
            throw new BusinessException(
                    ErrorCode.FORBIDDEN,
                    "只能读取当前用户自己的 AI 草稿"
            );
        }
        String body = storageService.readTextObject(row.getContentObjectKey(), 512 * 1024);
        return new AssistantPostContext(
                String.valueOf(row.getId()),
                row.getTitle(),
                row.getDescription(),
                body,
                parseStringList(row.getTags()),
                String.valueOf(row.getCreatorId()),
                row.getAuthorNickname(),
                row.getPublishTime(),
                row.getContentOrigin(),
                row.getContentSha256()
        );
    }

    @Transactional
    public void deleteOwnPost(long postId, long creatorId) {
        knowPostService.delete(creatorId, postId);
    }

    @Transactional(readOnly = true)
    public List<AssistantOwnPostSummary> listOwnPosts(
            long creatorId,
            int limit,
            int offset
    ) {
        int boundedLimit = Math.min(Math.max(limit, 1), 100);
        int boundedOffset = Math.max(offset, 0);
        return knowPostMapper.listOwnPostsForAssistant(
                        creatorId,
                        boundedLimit,
                        boundedOffset
                )
                .stream()
                .map(post -> new AssistantOwnPostSummary(
                        String.valueOf(post.getId()),
                        post.getTitle(),
                        post.getStatus(),
                        post.getVisible(),
                        post.getCreateTime(),
                        post.getPublishTime()
                ))
                .toList();
    }

    @Transactional
    public AssistantBatchDeleteResponse deleteOwnPosts(
            List<String> rawPostIds,
            long creatorId
    ) {
        List<Long> postIds = rawPostIds.stream()
                .map(value -> parsePositiveId(value, "帖子 ID"))
                .distinct()
                .toList();
        int deleted = 0;
        int alreadyDeleted = 0;
        for (Long postId : postIds) {
            KnowPost post = knowPostMapper.findById(postId);
            if (post == null || post.getCreatorId() == null
                    || post.getCreatorId() != creatorId) {
                throw new BusinessException(
                        ErrorCode.FORBIDDEN,
                        "批量删除清单包含不属于当前用户的帖子"
                );
            }
            if ("deleted".equals(post.getStatus())) {
                alreadyDeleted++;
                continue;
            }
            knowPostService.delete(creatorId, postId);
            deleted++;
        }
        return new AssistantBatchDeleteResponse(
                postIds.stream().map(String::valueOf).toList(),
                deleted,
                alreadyDeleted,
                "deleted"
        );
    }

    @Transactional(readOnly = true)
    public AssistantEngagementAnalytics analyzeEngagement(
            String topic,
            int days,
            int limit
    ) {
        String normalizedTopic = topic == null ? "" : topic.trim();
        if (normalizedTopic.length() > 100) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "分析主题不能超过100字");
        }
        int boundedDays = Math.min(Math.max(days, 1), 366);
        int boundedLimit = Math.min(Math.max(limit, 1), 20);
        Instant periodEnd = Instant.now();
        Instant periodStart = periodEnd.minus(boundedDays, ChronoUnit.DAYS);
        Map<String, Object> summary = knowPostMapper.analyzeEngagementForAssistant(
                normalizedTopic,
                periodStart
        );
        List<AssistantEngagementPost> topPosts = knowPostMapper
                .listTopEngagementPostsForAssistant(
                        normalizedTopic,
                        periodStart,
                        boundedLimit
                )
                .stream()
                .map(row -> new AssistantEngagementPost(
                        String.valueOf(row.get("id")),
                        text(row.get("title")),
                        text(row.get("description")),
                        String.valueOf(row.get("authorId")),
                        text(row.get("authorNickname")),
                        instant(row.get("publishTime")),
                        number(row.get("commentCount"))
                ))
                .toList();
        List<AssistantContributorInsight> contributors = knowPostMapper
                .listTopContributorsForAssistant(
                        normalizedTopic,
                        periodStart,
                        boundedLimit
                )
                .stream()
                .map(row -> new AssistantContributorInsight(
                        String.valueOf(row.get("userId")),
                        text(row.get("nickname")),
                        number(row.get("commentCount"))
                ))
                .toList();
        return new AssistantEngagementAnalytics(
                normalizedTopic,
                periodStart,
                periodEnd,
                number(summary == null ? null : summary.get("publishedPostCount")),
                number(summary == null ? null : summary.get("commentCount")),
                number(summary == null ? null : summary.get("activeCreatorCount")),
                number(summary == null ? null : summary.get("interactingUserCount")),
                topPosts,
                contributors,
                List.of("published_posts", "comments", "creators", "commenters"),
                List.of("点赞和收藏当前由计数服务维护，尚未进入该分析快照")
        );
    }

    @Transactional
    public AssistantPublishResponse publishAiDraft(
            long postId,
            long creatorId,
            String expectedContentSha256
    ) {
        KnowPost post = knowPostMapper.findById(postId);
        if (post == null || post.getCreatorId() == null
                || post.getCreatorId() != creatorId) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "AI 草稿不存在或不属于该用户");
        }
        if (!"AI_ASSISTED".equalsIgnoreCase(post.getContentOrigin())) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "助手只能发布 AI 创作草稿");
        }
        if (post.getContentSha256() == null
                || !post.getContentSha256().equalsIgnoreCase(expectedContentSha256)) {
            throw new BusinessException(
                    ErrorCode.BAD_REQUEST,
                    "草稿内容已变化，原批准或定时任务已失效，请重新确认"
            );
        }
        if ("published".equals(post.getStatus())) {
            return new AssistantPublishResponse(String.valueOf(postId), "published", true);
        }
        if (!"draft".equals(post.getStatus())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿当前状态不可发布");
        }
        String status = knowPostService.publish(creatorId, postId);
        return new AssistantPublishResponse(String.valueOf(postId), status, false);
    }

    @Transactional
    public CommentResponse replyToComment(AssistantCommentReplyRequest request) {
        long postId = parsePositiveId(request.postId(), "帖子 ID");
        long parentCommentId = parsePositiveId(request.parentCommentId(), "评论 ID");
        Long existingCommentId = provenanceMapper.findCommentIdByRunId(
                request.assistantRunId()
        );
        if (existingCommentId != null) {
            return commentService.get(existingCommentId, null);
        }
        KnowPost post = knowPostMapper.findById(postId);
        if (post == null || !"published".equals(post.getStatus())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "帖子不存在或不可回复");
        }
        userMapper.ensureAssistantUser();
        User assistantUser = userMapper.findByZgId("zhiguang-assistant");
        if (assistantUser == null || assistantUser.getId() == null) {
            throw new BusinessException(ErrorCode.INTERNAL_ERROR, "助手系统账号初始化失败");
        }
        CommentResponse created = commentService.create(
                assistantUser.getId(),
                new CommentCreateRequest(postId, parentCommentId, request.content())
        );
        provenanceMapper.insert(
                Long.parseLong(created.id()),
                request.assistantRunId(),
                postId,
                post.getContentSha256()
        );
        return commentService.get(Long.parseLong(created.id()), null);
    }

    private long parsePositiveId(String value, String label) {
        try {
            long parsed = Long.parseLong(value);
            if (parsed <= 0) {
                throw new NumberFormatException();
            }
            return parsed;
        } catch (NumberFormatException ex) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, label + "格式不正确");
        }
    }

    private List<String> parseStringList(String value) {
        if (value == null || value.isBlank()) {
            return List.of();
        }
        try {
            return objectMapper.readValue(value, new TypeReference<>() {});
        } catch (Exception ignored) {
            return List.of();
        }
    }

    private long number(Object value) {
        return value instanceof Number number ? number.longValue() : 0L;
    }

    private String text(Object value) {
        return value == null ? "" : String.valueOf(value);
    }

    private Instant instant(Object value) {
        if (value instanceof Instant instant) {
            return instant;
        }
        if (value instanceof java.sql.Timestamp timestamp) {
            return timestamp.toInstant();
        }
        return null;
    }
}
