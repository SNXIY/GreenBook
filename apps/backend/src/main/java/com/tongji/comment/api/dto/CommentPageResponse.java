package com.tongji.comment.api.dto;

import java.util.List;

public record CommentPageResponse(
        List<CommentResponse> items,
        String nextCursor,
        boolean hasMore
) {
}
