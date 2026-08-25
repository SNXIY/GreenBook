package com.tongji.search.rag;

import com.tongji.knowpost.event.PostLifecycleEvent;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.search.EmbeddingService;
import com.tongji.search.PostSearchDocument;
import com.tongji.search.PostSearchDocumentService;
import com.tongji.search.SearchProviderException;
import com.tongji.search.rag.config.RagProperties;
import com.tongji.search.rag.mapper.PostChunkMapper;
import com.tongji.search.rag.model.PostChunk;
import com.tongji.search.rag.service.PostChunker;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.util.List;

/** Projects canonical post content into MySQL chunk rows and Qdrant chunks. */
@Service
public class PostChunkProjectionService {
    private final PostChunkMapper chunkMapper;
    private final PostSearchDocumentService documents;
    private final KnowPostMapper postMapper;
    private final PostChunker chunker;
    private final QdrantChunkClient qdrant;
    private final EmbeddingService embedding;
    private final RagProperties properties;
    private final RagProjectionMetrics metrics = new RagProjectionMetrics();

    public PostChunkProjectionService(PostChunkMapper chunkMapper,
                                      PostSearchDocumentService documents,
                                      KnowPostMapper postMapper,
                                      PostChunker chunker,
                                      QdrantChunkClient qdrant,
                                      EmbeddingService embedding,
                                      RagProperties properties) {
        this.chunkMapper = chunkMapper;
        this.documents = documents;
        this.postMapper = postMapper;
        this.chunker = chunker;
        this.qdrant = qdrant;
        this.embedding = embedding;
        this.properties = properties;
    }

    public int apply(PostLifecycleEvent event) {
        if (!properties.enabled()) return 0;
        try {
            KnowPost post = documents.find(event.postId());
            if (isStale(post, event)) {
                metrics.stale();
                return 0;
            }
            if (!documents.searchable(post)) {
                qdrant.deleteByPostId(event.postId());
                chunkMapper.deleteByPostId(event.postId());
                metrics.deleted();
                return 0;
            }

            PostSearchDocument document = documents.build(post);
            String content = document.content() == null ? "" : document.content();
            if (content.getBytes(StandardCharsets.UTF_8).length > properties.maxSourceBytes()) {
                throw new SearchProviderException("canonical post content exceeds RAG source limit post="
                        + event.postId());
            }
            if (embedding.dimension() != 384) {
                throw new SearchProviderException("RAG chunk embedding dimension must be 384, actual="
                        + embedding.dimension());
            }
            List<PostChunk> chunks = chunker.chunk(
                    event.postId(),
                    event.eventVersion(),
                    content,
                    embedding.model(),
                    embedding.vectorVersion(),
                    embedding.dimension());

            // Stable chunk ids plus the Qdrant version guard make this safe to
            // replay. The canonical post version guard above prevents an old
            // lifecycle event from deleting newer chunks.
            qdrant.deleteByPostId(event.postId());
            chunkMapper.deleteByPostId(event.postId());
            if (!chunks.isEmpty()) chunkMapper.insertBatch(chunks);
            for (PostChunk chunk : chunks) {
                qdrant.upsert(chunk, embedding.embedDocument(
                        chunk.textForEmbedding(document.title(), document.tags(), document.description())));
            }
            metrics.applied();
            return chunks.size();
        } catch (RuntimeException e) {
            metrics.failure();
            throw e;
        }
    }

    private boolean isStale(KnowPost post, PostLifecycleEvent event) {
        if (post != null && post.getEventVersion() != null
                && post.getEventVersion() > event.eventVersion()) return true;
        if (post == null) {
            Long stored = chunkMapper.findMaxEventVersion(event.postId());
            return stored != null && stored > event.eventVersion();
        }
        return false;
    }

    public RagProjectionMetrics metrics() {
        return metrics;
    }
}
