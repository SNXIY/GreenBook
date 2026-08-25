package com.tongji.search;

import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.storage.OssStorageService;
import org.springframework.stereotype.Service;

/** Builds a projection document from MySQL plus canonical content storage. */
@Service
public class PostSearchDocumentService {
    private final KnowPostMapper postMapper;
    private final OssStorageService storage;

    public PostSearchDocumentService(KnowPostMapper postMapper, OssStorageService storage) {
        this.postMapper = postMapper;
        this.storage = storage;
    }

    public KnowPost find(long postId) {
        return postMapper.findById(postId);
    }

    public boolean searchable(KnowPost post) {
        return post != null
                && "published".equals(post.getStatus())
                && "public".equals(post.getVisible());
    }

    public PostSearchDocument build(KnowPost post) {
        if (post == null || post.getId() == null) return null;
        String content = "";
        if (post.getContentObjectKey() != null && !post.getContentObjectKey().isBlank()) {
            try {
                content = storage.readTextObject(post.getContentObjectKey(), 1024 * 1024);
            } catch (Exception e) {
                throw new SearchProviderException("canonical post content unavailable for projection post="
                        + post.getId(), e);
            }
        }
        return new PostSearchDocument(
                post.getId(),
                post.getCreatorId(),
                post.getTitle(),
                post.getDescription(),
                post.getTags(),
                content,
                post.getStatus(),
                post.getVisible(),
                post.getPublishTime(),
                post.getUpdateTime(),
                post.getEventVersion() == null ? 0L : post.getEventVersion()
        );
    }
}
