package com.tongji.search.projection;

import com.tongji.knowpost.event.PostLifecycleEvent;
import com.tongji.knowpost.event.PostLifecycleEventType;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import org.springframework.stereotype.Service;

import java.time.Instant;

/** Explicit operator/test entry point for rebuilding projections from MySQL truth. */
@Service
public class SearchProjectionRebuildService {
    private final KnowPostMapper postMapper;
    private final ElasticsearchPostProjectionConsumer elasticsearch;
    private final QdrantPostProjectionConsumer qdrant;

    public SearchProjectionRebuildService(KnowPostMapper postMapper,
                                          ElasticsearchPostProjectionConsumer elasticsearch,
                                          QdrantPostProjectionConsumer qdrant) {
        this.postMapper = postMapper;
        this.elasticsearch = elasticsearch;
        this.qdrant = qdrant;
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
                PostLifecycleEvent event = new PostLifecycleEvent(
                        0L, post.getId(), version, PostLifecycleEventType.PostUpdated,
                        post.getStatus(), post.getVisible(), post.getContentObjectKey(),
                        post.getContentEtag(), post.getContentSha256(),
                        post.getUpdateTime() == null ? Instant.now() : post.getUpdateTime(),
                        post.getCreatorId(), "rebuild");
                elasticsearch.apply(event);
                qdrant.apply(event);
                rebuilt++;
            }
            offset += posts.size();
            if (posts.size() < boundedBatch) break;
        }
        return rebuilt;
    }
}
