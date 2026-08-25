package com.tongji.search.rag.projection;

import com.tongji.knowpost.event.PostLifecycleEvent;
import com.tongji.knowpost.event.PostLifecycleEventType;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.search.rag.PostChunkProjectionService;
import org.springframework.stereotype.Service;

import java.time.Instant;

/** Explicit rebuild/backfill entry point for the chunk projection only. */
@Service
public class PostChunkRebuildService {
    private final KnowPostMapper postMapper;
    private final PostChunkProjectionService projection;

    public PostChunkRebuildService(KnowPostMapper postMapper,
                                   PostChunkProjectionService projection) {
        this.postMapper = postMapper;
        this.projection = projection;
    }

    public int rebuildPublic(int batchSize) {
        int boundedBatch = Math.min(Math.max(batchSize, 1), 500);
        int offset = 0;
        int rebuilt = 0;
        while (true) {
            var posts = postMapper.listPublicForSearchRebuild(boundedBatch, offset);
            if (posts == null || posts.isEmpty()) break;
            for (KnowPost post : posts) {
                long version = post.getEventVersion() == null ? 0L : post.getEventVersion();
                projection.apply(new PostLifecycleEvent(
                        0L, post.getId(), version, PostLifecycleEventType.PostUpdated,
                        post.getStatus(), post.getVisible(), post.getContentObjectKey(),
                        post.getContentEtag(), post.getContentSha256(),
                        post.getUpdateTime() == null ? Instant.now() : post.getUpdateTime(),
                        post.getCreatorId(), "rebuild"));
                rebuilt++;
            }
            offset += posts.size();
            if (posts.size() < boundedBatch) break;
        }
        return rebuilt;
    }
}
