package com.tongji.search;

import com.tongji.agentfacade.api.dto.SearchPageResponse;
import com.tongji.agentfacade.api.dto.SearchPostItem;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.search.config.SearchProperties;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Domain-level relevant search provider. BM25, dense retrieval and RRF remain
 * infrastructure details; the Agent still calls one community search tool.
 */
@Service
public class HybridSearchService {
    private static final int DEFAULT_RRF_K = 60;

    private final MySqlSearchProvider mysql;
    private final ElasticsearchPostClient elasticsearch;
    private final QdrantPostClient qdrant;
    private final EmbeddingService embedding;
    private final SearchProperties properties;
    private final SearchProjectionMetrics projectionMetrics;

    public HybridSearchService(MySqlSearchProvider mysql,
                               ElasticsearchPostClient elasticsearch,
                               QdrantPostClient qdrant,
                               EmbeddingService embedding,
                               SearchProperties properties,
                               SearchProjectionMetrics projectionMetrics) {
        this.mysql = mysql;
        this.elasticsearch = elasticsearch;
        this.qdrant = qdrant;
        this.embedding = embedding;
        this.properties = properties;
        this.projectionMetrics = projectionMetrics;
    }

    @Transactional(readOnly = true)
    public SearchPageResponse search(String query, String sort, int page, int size) {
        String normalized = query == null ? "" : query.trim();
        String normalizedSort = normalizeSort(sort);
        if (!"relevant".equals(normalizedSort) || normalized.isBlank() || !properties.hybridEnabled()) {
            return mysql.search(normalized, normalizedSort, page, size);
        }

        int boundedPage = Math.max(page, 1);
        int boundedSize = Math.min(Math.max(size, 1), 50);
        int candidateLimit = Math.min(200, Math.max(properties.candidateLimit(),
                (int) Math.min((long) boundedPage * boundedSize, 200L)));
        int lexicalLimit = boundedCandidateLimit(properties.bm25TopN(), candidateLimit);
        int denseLimit = boundedCandidateLimit(properties.denseTopN(), candidateLimit);
        int rrfK = properties.rrfK() > 0 ? properties.rrfK() : DEFAULT_RRF_K;
        long started = System.nanoTime();
        try {
            List<LexicalSearchHit> lexical = elasticsearch.search(normalized, lexicalLimit);
            List<DenseSearchHit> dense = qdrant.search(embedding.embedQuery(normalized), denseLimit);
            List<RankedCandidate> fused = rrf(lexical, dense, rrfK);
            if (fused.isEmpty()) {
                if (mysql.count(normalized) > 0) projectionMetrics.missing();
                return degraded(mysql.search(normalized, "relevant", page, size));
            }

            List<Long> ids = fused.stream().map(RankedCandidate::postId).toList();
            List<KnowPostDetailRow> rows = mysql.loadPublicByIds(ids);
            if (rows.size() < ids.size()) projectionMetrics.missing();
            Map<Long, KnowPostDetailRow> byId = new LinkedHashMap<>();
            for (KnowPostDetailRow row : rows) byId.put(row.getId(), row);
            List<RankedCandidate> filtered = new ArrayList<>();
            for (RankedCandidate candidate : fused) {
                KnowPostDetailRow row = byId.get(candidate.postId());
                // The MySQL load already enforces published/public visibility. Do not
                // apply a second lexical predicate here: it would discard the
                // Dense-only semantic candidates that RRF is meant to contribute.
                if (row != null) {
                    filtered.add(candidate);
                }
            }
            long total = mysql.count(normalized);
            int offset = (int) Math.min(((long) boundedPage - 1L) * boundedSize, Integer.MAX_VALUE - 1L);
            List<SearchPostItem> items = pageItems(filtered, byId, offset, boundedSize);
            return new SearchPageResponse(items, boundedPage, boundedSize, total,
                    (int) Math.min(Integer.MAX_VALUE, (total + boundedSize - 1) / boundedSize),
                    (long) offset + items.size() < total, "relevant", "hybrid_rrf", false);
        } catch (SearchProviderException e) {
            SearchPageResponse fallback = mysql.search(normalized, "relevant", page, size);
            return degraded(fallback);
        } finally {
            long elapsedMs = (System.nanoTime() - started) / 1_000_000L;
            if (elapsedMs > properties.requestTimeoutMs() * 2L) projectionMetrics.failure();
        }
    }

    private List<SearchPostItem> pageItems(List<RankedCandidate> filtered,
                                           Map<Long, KnowPostDetailRow> byId,
                                           int offset,
                                           int size) {
        int end = offset < filtered.size()
                ? (int) Math.min((long) offset + size, filtered.size()) : offset;
        if (end <= offset) return List.of();
        List<RankedCandidate> page = filtered.subList(offset, end);
        if (!properties.businessRerankEnabled()) {
            return page.stream().map(candidate -> mysql.toItem(byId.get(candidate.postId()))).toList();
        }
        List<RankedItem> ranked = filtered.stream()
                .map(candidate -> new RankedItem(candidate, mysql.toItem(byId.get(candidate.postId()))))
                .sorted(Comparator.comparingDouble((RankedItem value) -> value.finalScore(true)).reversed()
                        .thenComparingLong(value -> value.item().publishedAt() == null
                                ? Long.MIN_VALUE : -value.item().publishedAt().toEpochMilli()))
                .toList();
        return offset < ranked.size()
                ? ranked.subList(offset, Math.min(offset + size, ranked.size())).stream()
                .map(RankedItem::item).toList()
                : List.of();
    }

    private List<RankedCandidate> rrf(List<LexicalSearchHit> lexical,
                                      List<DenseSearchHit> dense,
                                      int rrfK) {
        Map<Long, Double> scores = new LinkedHashMap<>();
        for (LexicalSearchHit hit : lexical) {
            scores.merge(hit.postId(), 1.0 / (rrfK + hit.rank()), Double::sum);
        }
        for (DenseSearchHit hit : dense) {
            scores.merge(hit.postId(), 1.0 / (rrfK + hit.rank()), Double::sum);
        }
        return scores.entrySet().stream()
                .sorted(Map.Entry.<Long, Double>comparingByValue().reversed()
                        .thenComparingLong(Map.Entry::getKey))
                .map(entry -> new RankedCandidate(entry.getKey(), entry.getValue()))
                .toList();
    }

    private int boundedCandidateLimit(int configured, int fallback) {
        return configured > 0 ? Math.min(200, configured) : fallback;
    }

    private SearchPageResponse degraded(SearchPageResponse response) {
        return new SearchPageResponse(response.items(), response.page(), response.size(), response.total(),
                response.totalPages(), response.hasMore(), response.sort(), "mysql_fallback", true);
    }

    private String normalizeSort(String sort) {
        return switch (sort == null ? "latest" : sort.toLowerCase()) {
            case "hot" -> "hot";
            case "relevant" -> "relevant";
            default -> "latest";
        };
    }

    private record RankedCandidate(long postId, double fusedScore) {}

    private record RankedItem(RankedCandidate candidate, SearchPostItem item) {
        private double finalScore(boolean businessRerank) {
            if (!businessRerank) return candidate.fusedScore();
            // Relevance is primary. Business quality is a bounded tie-breaker.
            double hot = item.hotScore() == null ? 0.0 : Math.max(0.0, item.hotScore());
            double qualityAdjustment = Math.min(0.001, Math.log1p(hot) * 0.0001);
            return candidate.fusedScore() + qualityAdjustment;
        }
    }
}
