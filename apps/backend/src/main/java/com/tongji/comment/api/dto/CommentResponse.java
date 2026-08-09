package com.tongji.comment.api.dto;

import java.time.Instant;

public record CommentResponse(
        String id,
        String postId,
        String parentId,
        String rootId,
        String userId,
        String authorNickname,
        String authorAvatar,
        String content,
        boolean top,
        int replyCount,
        long likeCount,
        boolean liked,
        boolean assistant,
        String assistantRunId,
        Instant createTime
) {
}
