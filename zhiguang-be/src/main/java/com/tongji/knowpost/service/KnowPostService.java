package com.tongji.knowpost.service;

import com.tongji.knowpost.api.dto.KnowPostDetailResponse;
import com.tongji.knowpost.api.dto.PostTaskItemResponse;
import com.tongji.knowpost.api.dto.PublishStatusResponse;

import java.util.List;

/**
 * 知文业务接口。
 */
public interface KnowPostService {

    long createDraft(long creatorId);

    long createAiDraft(long creatorId, String title, String bodyMarkdown, String description, String contentSha256);

    void confirmContent(long creatorId, long id, String objectKey, String etag, Long size, String sha256);

    void updateMetadata(long creatorId, long id, String title, Long tagId, List<String> tags, List<String> imgUrls, String visible, Boolean isTop, String description);

    /** @return reviewing | published */
    String publish(long creatorId, long id);

    PublishStatusResponse getPublishStatus(long creatorId, long id);

    List<PostTaskItemResponse> listTaskItems(long creatorId, int limit);

    String getContent(long creatorId, long id);

    void resumeAfterModerationTask(String moderationTaskId);

    void applyModerationResult(String moderationTaskId, String contentId,
                               String status, String finalAction, String reason);

    void updateTop(long creatorId, long id, boolean isTop);

    void updateVisibility(long creatorId, long id, String visible);

    void delete(long creatorId, long id);

    KnowPostDetailResponse getDetail(long id, Long currentUserIdNullable);
}
