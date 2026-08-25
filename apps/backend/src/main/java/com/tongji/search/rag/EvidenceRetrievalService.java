package com.tongji.search.rag;

import com.tongji.agentfacade.api.dto.SearchPageResponse;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.search.EmbeddingService;
import com.tongji.search.HybridSearchService;
import com.tongji.search.rag.api.EvidenceChunk;
import com.tongji.search.rag.api.KnowledgeEvidenceResponse;
import com.tongji.search.rag.config.RagProperties;
import com.tongji.search.rag.mapper.PostChunkMapper;
import com.tongji.search.rag.model.ChunkDenseSearchHit;
import com.tongji.search.rag.model.PostChunk;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Query -> existing post search -> candidate-scoped evidence chunk retrieval. */
@Service
public class EvidenceRetrievalService {
    private final HybridSearchService hybridSearch;
    private final QdrantChunkClient qdrant;
    private final PostChunkMapper chunkMapper;
    private final KnowPostMapper postMapper;
    private final EmbeddingService embedding;
    private final RagProperties properties;

    public EvidenceRetrievalService(HybridSearchService hybridSearch,
                                    QdrantChunkClient qdrant,
                                    PostChunkMapper chunkMapper,
                                    KnowPostMapper postMapper,
                                    EmbeddingService embedding,
                                    RagProperties properties) {
        this.hybridSearch = hybridSearch;
        this.qdrant = qdrant;
        this.chunkMapper = chunkMapper;
        this.postMapper = postMapper;
        this.embedding = embedding;
        this.properties = properties;
    }

    public KnowledgeEvidenceResponse retrieve(String question,
                                              Integer requestedTopPosts,
                                              Integer requestedTopChunks) {
        String normalized = question == null ? "" : question.trim();
        if (normalized.isBlank()) throw new IllegalArgumentException("question must not be blank");
        if (!properties.enabled()) return new KnowledgeEvidenceResponse(List.of(), 0, 0, 0, true);

        int topPosts = bound(requestedTopPosts, properties.candidatePosts(), 1, 20);
        int topChunks = bound(requestedTopChunks, properties.topChunks(), 1, 20);
        SearchPageResponse postResponse = hybridSearch.search(normalized, "relevant", 1, topPosts);
        List<Long> candidatePostIds = postResponse.items().stream()
                .map(item -> parseId(item.postId()))
                .filter(id -> id != null && id > 0)
                .toList();
        if (candidatePostIds.isEmpty()) {
            return new KnowledgeEvidenceResponse(List.of(), 0, 0, 0, postResponse.degraded());
        }

        long embeddingStarted = System.nanoTime();
        float[] vector = embedding.embedQuery(normalized);
        long embeddingLatencyMs = elapsedMs(embeddingStarted);

        long retrievalStarted = System.nanoTime();
        List<ChunkDenseSearchHit> hits = qdrant.search(vector, topChunks, candidatePostIds);
        Map<String, PostChunk> rows = loadChunkRows(hits);
        Map<Long, Integer> postRanks = new HashMap<>();
        for (int index = 0; index < candidatePostIds.size(); index++) {
            postRanks.putIfAbsent(candidatePostIds.get(index), index);
        }
        List<RankedEvidence> ranked = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (ChunkDenseSearchHit hit : hits) {
            if (!seen.add(hit.chunkId())) continue;
            PostChunk row = rows.get(hit.chunkId());
            if (row == null || row.getPostId() != hit.postId()
                    || !postRanks.containsKey(row.getPostId())) continue;
            KnowPost post = postMapper.findById(row.getPostId());
            if (!isCurrentPublicPost(post, row)) continue;
            ranked.add(new RankedEvidence(
                    new EvidenceChunk(
                            row.getChunkId(),
                            String.valueOf(row.getPostId()),
                            post.getTitle(),
                            row.getContent(),
                            hit.score(),
                            row.getStartOffset(),
                            row.getEndOffset(),
                            row.getEventVersion(),
                            row.getUpdatedAt()),
                    postRanks.get(row.getPostId())));
        }
        ranked.sort(Comparator
                .comparingDouble((RankedEvidence value) -> value.chunk().score()).reversed()
                .thenComparingInt(RankedEvidence::postRank)
                .thenComparing((RankedEvidence value) ->
                        value.chunk().updatedAt() == null ? Instant.EPOCH : value.chunk().updatedAt(),
                        Comparator.reverseOrder())
                .thenComparing(value -> value.chunk().chunkId()));
        List<EvidenceChunk> evidence = ranked.stream()
                .limit(topChunks)
                .map(RankedEvidence::chunk)
                .toList();
        return new KnowledgeEvidenceResponse(
                evidence,
                candidatePostIds.size(),
                embeddingLatencyMs,
                elapsedMs(retrievalStarted),
                postResponse.degraded());
    }

    private Map<String, PostChunk> loadChunkRows(List<ChunkDenseSearchHit> hits) {
        List<String> ids = hits.stream().map(ChunkDenseSearchHit::chunkId).distinct().toList();
        if (ids.isEmpty()) return Map.of();
        Map<String, PostChunk> result = new HashMap<>();
        for (PostChunk row : chunkMapper.findByIds(ids)) result.put(row.getChunkId(), row);
        return result;
    }

    private boolean isCurrentPublicPost(KnowPost post, PostChunk row) {
        return post != null
                && "published".equals(post.getStatus())
                && "public".equals(post.getVisible())
                && (post.getEventVersion() == null || post.getEventVersion() == row.getEventVersion());
    }

    private Long parseId(String value) {
        try {
            return value == null ? null : Long.parseLong(value);
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    private int bound(Integer value, int fallback, int min, int max) {
        return Math.min(max, Math.max(min, value == null ? fallback : value));
    }

    private long elapsedMs(long started) {
        return (System.nanoTime() - started) / 1_000_000L;
    }

    private record RankedEvidence(EvidenceChunk chunk, int postRank) {}
}
