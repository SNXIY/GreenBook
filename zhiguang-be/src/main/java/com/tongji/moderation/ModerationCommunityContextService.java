package com.tongji.moderation;

import com.tongji.comment.mapper.CommentMapper;
import com.tongji.comment.model.CommentRow;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.moderation.api.dto.ModerationCommunityContentRecord;
import com.tongji.moderation.api.dto.ModerationCommunityContentSnapshot;
import com.tongji.moderation.api.dto.ModerationReportEvidence;
import com.tongji.moderation.api.dto.ModerationViolationRecord;
import com.tongji.storage.OssStorageService;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Locale;

@Service
@RequiredArgsConstructor
public class ModerationCommunityContextService {
    private static final int MAX_CONTENT_BYTES = 512 * 1024;

    private final KnowPostMapper knowPostMapper;
    private final CommentMapper commentMapper;
    private final OssStorageService storageService;

    @Transactional(readOnly = true)
    public ModerationCommunityContentSnapshot getContentContext(long contentId) {
        KnowPost post = knowPostMapper.findById(contentId);
        if (post != null) {
            ModerationCommunityContentRecord record = toPostRecord(post);
            return new ModerationCommunityContentSnapshot(record, record, false);
        }

        CommentRow comment = commentMapper.findById(contentId);
        if (comment == null) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "审核内容不存在");
        }
        KnowPost parentPost = knowPostMapper.findById(comment.getPostId());
        return new ModerationCommunityContentSnapshot(
                toCommentRecord(comment),
                parentPost == null ? null : toPostRecord(parentPost),
                comment.getParentId() != null
        );
    }

    @Transactional(readOnly = true)
    public ModerationCommunityContentRecord getParentComment(long contentId) {
        CommentRow comment = commentMapper.findById(contentId);
        if (comment == null || comment.getParentId() == null) {
            return null;
        }
        CommentRow parent = commentMapper.findById(comment.getParentId());
        return parent == null ? null : toCommentRecord(parent);
    }

    @Transactional(readOnly = true)
    public List<ModerationCommunityContentRecord> getConversationContext(long contentId, int limit) {
        long postId = resolvePostId(contentId);
        int boundedLimit = Math.min(Math.max(limit, 1), 20);
        return commentMapper.listConversationForModeration(postId, boundedLimit)
                .stream()
                .map(this::toCommentRecord)
                .toList();
    }

    @Transactional(readOnly = true)
    public List<ModerationCommunityContentRecord> getAuthorRecentContents(long authorId, int limit) {
        int boundedLimit = Math.min(Math.max(limit, 1), 20);
        return knowPostMapper.listRecentForModeration(authorId, boundedLimit)
                .stream()
                .map(this::toPostRecord)
                .toList();
    }

    @Transactional(readOnly = true)
    public List<ModerationViolationRecord> getAuthorViolationHistory(long authorId) {
        return knowPostMapper.listRejectedForModeration(authorId, 20)
                .stream()
                .map(post -> new ModerationViolationRecord(
                        String.valueOf(post.getId()),
                        inferRiskType(post.getModerationReason()),
                        "REJECT",
                        nonBlank(post.getModerationReason(), "内容审核未通过"),
                        post.getUpdateTime()
                ))
                .toList();
    }

    @Transactional(readOnly = true)
    public List<ModerationReportEvidence> getContentReports(long contentId) {
        resolvePostId(contentId);
        // The community platform does not have a report persistence table yet.
        // Returning an empty collection is truthful and keeps the agent evidence contract stable.
        return List.of();
    }

    private long resolvePostId(long contentId) {
        if (knowPostMapper.findById(contentId) != null) {
            return contentId;
        }
        CommentRow comment = commentMapper.findById(contentId);
        if (comment == null || comment.getPostId() == null) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "审核内容不存在");
        }
        return comment.getPostId();
    }

    private ModerationCommunityContentRecord toPostRecord(KnowPost post) {
        return new ModerationCommunityContentRecord(
                String.valueOf(post.getId()),
                "POST",
                String.valueOf(post.getCreatorId()),
                readPostContent(post),
                post.getTitle(),
                post.getStatus(),
                post.getCreateTime()
        );
    }

    private ModerationCommunityContentRecord toCommentRecord(CommentRow comment) {
        return new ModerationCommunityContentRecord(
                String.valueOf(comment.getId()),
                "COMMENT",
                String.valueOf(comment.getUserId()),
                nonBlank(comment.getContent(), ""),
                null,
                comment.getStatus(),
                comment.getCreateTime()
        );
    }

    private String readPostContent(KnowPost post) {
        if (post.getContentObjectKey() != null && !post.getContentObjectKey().isBlank()) {
            try {
                return storageService.readTextObject(post.getContentObjectKey(), MAX_CONTENT_BYTES);
            } catch (RuntimeException ignored) {
                // The task already carries the submitted body. Context enrichment must remain available
                // even when a historical storage object is temporarily unavailable.
            }
        }
        String title = nonBlank(post.getTitle(), "");
        String description = nonBlank(post.getDescription(), "");
        return (title + "\n" + description).trim();
    }

    private String inferRiskType(String reason) {
        String normalized = nonBlank(reason, "").toLowerCase(Locale.ROOT);
        if (normalized.contains("隐私") || normalized.contains("手机号")
                || normalized.contains("身份证")) {
            return "PRIVACY";
        }
        if (normalized.contains("广告") || normalized.contains("引流")
                || normalized.contains("推广")) {
            return "ADVERTISING";
        }
        return "ABUSE";
    }

    private String nonBlank(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value;
    }
}
