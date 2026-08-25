package com.tongji.search;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.agentfacade.api.dto.SearchPageResponse;
import com.tongji.agentfacade.api.dto.SearchPostItem;
import com.tongji.comment.api.dto.CommentPageResponse;
import com.tongji.comment.service.CommentService;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.counter.service.CounterService;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.search.config.SearchProperties;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** Existing MySQL search, retained as the correctness baseline and read fallback. */
@Service
public class MySqlSearchProvider {
    private final KnowPostMapper mapper;
    private final CounterService counterService;
    private final CommentService commentService;
    private final SearchProperties properties;
    private final ObjectMapper objectMapper = new ObjectMapper();

    public MySqlSearchProvider(KnowPostMapper mapper,
                               CounterService counterService,
                               CommentService commentService,
                               SearchProperties properties) {
        this.mapper = mapper;
        this.counterService = counterService;
        this.commentService = commentService;
        this.properties = properties;
    }

    public SearchPageResponse search(String query, String sort, int page, int size) {
        int boundedSize = Math.min(Math.max(size, 1), 50);
        int boundedPage = Math.max(page, 1);
        String normalizedQuery = query == null ? "" : query.trim();
        if (normalizedQuery.length() > 100) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "search query exceeds 100 characters");
        }
        String normalizedSort = normalizeSort(sort);
        List<String> tokens = tokenize(normalizedQuery);
        long total = tokens.isEmpty()
                ? mapper.countPublicForAgent(normalizedQuery)
                : mapper.countPublicForAgentTokens(normalizedQuery, tokens);
        long rawOffset = ((long) boundedPage - 1L) * boundedSize;
        int offset = (int) Math.min(rawOffset, Integer.MAX_VALUE - 1L);
        boolean candidateTruncated = false;
        int fetchOffset = "relevant".equals(normalizedSort) ? offset : 0;
        int fetchLimit;
        if ("relevant".equals(normalizedSort)) {
            fetchLimit = boundedSize;
        } else {
            fetchLimit = (int) Math.min(Math.max(total, boundedSize), properties.maxHotCandidates());
            candidateTruncated = total > fetchLimit;
        }
        List<KnowPostDetailRow> rows = tokens.isEmpty()
                ? mapper.searchPublicForAgent(normalizedQuery, fetchLimit, fetchOffset)
                : mapper.searchPublicForAgentTokens(normalizedQuery, tokens, fetchLimit, fetchOffset);

        List<SearchPostItem> items = rows.stream().map(this::toItem).toList();
        List<SearchPostItem> sorted = items;
        if ("hot".equals(normalizedSort) || "latest".equals(normalizedSort)) {
            sorted = items.stream().sorted((a, b) -> Double.compare(
                    b.hotScore() == null ? 0.0 : b.hotScore(),
                    a.hotScore() == null ? 0.0 : a.hotScore())).toList();
            if ("latest".equals(normalizedSort)) {
                sorted = items.stream().sorted((a, b) -> {
                    Instant ta = a.publishedAt() == null ? Instant.EPOCH : a.publishedAt();
                    Instant tb = b.publishedAt() == null ? Instant.EPOCH : b.publishedAt();
                    return tb.compareTo(ta);
                }).toList();
            }
            if (offset < sorted.size()) {
                int end = (int) Math.min((long) offset + boundedSize, sorted.size());
                sorted = sorted.subList(offset, end);
            } else {
                sorted = List.of();
            }
        }
        return new SearchPageResponse(
                sorted, boundedPage, boundedSize, total,
                (int) Math.min(Integer.MAX_VALUE, (total + boundedSize - 1) / boundedSize),
                (long) offset + sorted.size() < total,
                normalizedSort,
                candidateTruncated ? "mysql_baseline_truncated" : "mysql",
                candidateTruncated);
    }

    public long count(String query) {
        String normalized = query == null ? "" : query.trim();
        List<String> tokens = tokenize(normalized);
        return tokens.isEmpty()
                ? mapper.countPublicForAgent(normalized)
                : mapper.countPublicForAgentTokens(normalized, tokens);
    }

    public List<KnowPostDetailRow> loadPublicByIds(List<Long> ids) {
        List<KnowPostDetailRow> rows = new ArrayList<>();
        for (Long id : ids) {
            if (id == null) continue;
            KnowPostDetailRow row = mapper.findDetailById(id);
            if (row != null && "published".equals(row.getStatus()) && "public".equals(row.getVisible())) {
                rows.add(row);
            }
        }
        return rows;
    }

    /** Keep hybrid candidate rows consistent with the MySQL truth/count predicate. */
    public boolean matchesQuery(KnowPostDetailRow row, String query) {
        if (row == null || !"published".equals(row.getStatus()) || !"public".equals(row.getVisible())) {
            return false;
        }
        String normalized = query == null ? "" : query.trim().toLowerCase(Locale.ROOT);
        if (normalized.isEmpty()) {
            return true;
        }
        List<String> tokens = tokenize(normalized);
        if (tokens.isEmpty()) {
            return containsSearchField(row, normalized);
        }
        return tokens.stream().anyMatch(token -> containsSearchField(row, token));
    }

    public SearchPostItem toItem(KnowPostDetailRow row) {
        Map<String, Long> counts = counterService.getCounts("knowpost",
                String.valueOf(row.getId()), List.of("like", "fav"));
        long commentCount = 0;
        try {
            CommentPageResponse page = commentService.list(row.getId(), null, null, 1, null);
            commentCount = page.items().size();
        } catch (Exception ignored) {
            // Counters are quality signals only; MySQL post truth remains readable.
        }
        long likeCount = counts.getOrDefault("like", 0L);
        long favoriteCount = counts.getOrDefault("fav", 0L);
        long ageMinutes = Math.max(1, ChronoUnit.MINUTES.between(
                row.getPublishTime() == null ? Instant.now() : row.getPublishTime(), Instant.now()));
        double hotScore = Math.log(1 + likeCount * 2 + favoriteCount + commentCount * 1.5)
                / Math.log(ageMinutes + 2);
        return new SearchPostItem(String.valueOf(row.getId()),
                row.getCreatorId() == null ? null : String.valueOf(row.getCreatorId()),
                row.getTitle(), row.getDescription(), parseTags(row.getTags()),
                likeCount, commentCount, favoriteCount, row.getPublishTime(), hotScore);
    }

    private List<String> tokenize(String query) {
        if (query == null || query.isBlank()) return List.of();
        return java.util.Arrays.stream(query.toLowerCase(Locale.ROOT)
                        .replaceAll("[\\p{P}\\p{S}]+", " ").trim().split("\\s+"))
                .filter(value -> !value.isBlank())
                .distinct()
                .toList();
    }

    private List<String> parseTags(String tags) {
        if (tags == null || tags.isBlank()) return List.of();
        try {
            return objectMapper.readValue(tags, new TypeReference<List<String>>() {});
        } catch (Exception ignored) {
            return List.of();
        }
    }

    private boolean containsSearchField(KnowPostDetailRow row, String value) {
        return contains(row.getTitle(), value)
                || contains(row.getDescription(), value)
                || contains(row.getTags(), value);
    }

    private boolean contains(String field, String value) {
        return field != null && field.toLowerCase(Locale.ROOT).contains(value);
    }

    private String normalizeSort(String sort) {
        return switch (sort == null ? "latest" : sort.toLowerCase(Locale.ROOT)) {
            case "hot" -> "hot";
            case "relevant" -> "relevant";
            default -> "latest";
        };
    }
}
