package com.tongji.comment.service;

import com.tongji.comment.api.dto.CommentCreateRequest;
import com.tongji.comment.api.dto.CommentPageResponse;
import com.tongji.comment.api.dto.CommentResponse;

import java.util.List;

public interface CommentService {
    CommentResponse create(long userId, CommentCreateRequest request);

    CommentResponse get(long commentId, Long currentUserId);

    CommentPageResponse list(long postId, Long parentId, Long cursor, int size, Long currentUserId);

    List<CommentResponse> hot(long postId, int size, Long currentUserId);

    void delete(long userId, long commentId);

    void updateTop(long userId, long commentId, boolean top);

    void refreshHotRank(long commentId);
}
