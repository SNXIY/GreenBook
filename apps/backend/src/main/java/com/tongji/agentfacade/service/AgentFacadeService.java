package com.tongji.agentfacade.service;

import com.tongji.agentfacade.api.dto.*;
import com.tongji.agentfacade.contract.DraftMetadataContract;
import com.tongji.agentfacade.mapper.ScheduledPublicationMapper;
import com.tongji.comment.api.dto.CommentCreateRequest;
import com.tongji.comment.api.dto.CommentPageResponse;
import com.tongji.comment.api.dto.CommentResponse;
import com.tongji.comment.service.CommentService;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.counter.service.CounterService;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.relation.mapper.RelationMapper;
import com.tongji.storage.OssStorageService;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

@Service
@RequiredArgsConstructor
public class AgentFacadeService {

    private final KnowPostMapper knowPostMapper;
    private final KnowPostService knowPostService;
    private final CommentService commentService;
    private final CounterService counterService;
    private final RelationMapper relationMapper;
    private final OssStorageService ossStorageService;
    private final ScheduledPublicationMapper scheduledPublicationMapper;

    @Transactional(readOnly = true)
    public SearchPageResponse searchPosts(String query, String sort, int page, int size) {
        int boundedSize = Math.min(Math.max(size, 1), 50);
        int boundedPage = Math.max(page, 1);
        String normalizedQuery = query != null ? query.trim() : "";

        List<KnowPostDetailRow> rows;
        if (normalizedQuery.isEmpty()) {
            normalizedQuery = "";
        } else if (normalizedQuery.length() > 100) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "搜索关键词不能超过100字");
        }
        List<String> tokens = tokenizeSearchQuery(normalizedQuery);
        if (tokens.isEmpty()) {
            rows = knowPostMapper.searchPublicForAgent(normalizedQuery, Math.min(boundedSize * 3, 150));
        } else {
            // Token-aware retrieval: OR recall with field-weighted relevance
            // ranking, so multi-word queries ("Java 后端 面试") hit posts
            // matching any token instead of requiring the exact phrase.
            rows = knowPostMapper.searchPublicForAgentTokens(
                    normalizedQuery, tokens, Math.min(boundedSize * 3, 150));
        }

        List<SearchPostItem> items = rows.stream()
                .map(row -> {
                    Map<String, Long> counts = counterService.getCounts("knowpost",
                            String.valueOf(row.getId()), List.of("like", "fav"));
                    long commentCount = 0;
                    try {
                        commentCount = commentService.list(row.getId(), null, null, 1, null).items().size();
                    } catch (Exception ignored) {}

                    double hotScore = 0.0;
                    try {
                        long likeCount = counts.getOrDefault("like", 0L);
                        long favCount = counts.getOrDefault("fav", 0L);
                        long ageMinutes = Math.max(1,
                                ChronoUnit.MINUTES.between(
                                        row.getPublishTime() != null ? row.getPublishTime() : Instant.now(),
                                        Instant.now()));
                        hotScore = Math.log(1 + likeCount * 2 + favCount + commentCount * 1.5)
                                / Math.log(ageMinutes + 2);
                    } catch (Exception ignored) {}

                    // Sort by hotScore if requested
                    return new SearchPostItem(
                            String.valueOf(row.getId()),
                            row.getCreatorId() != null ? String.valueOf(row.getCreatorId()) : null,
                            row.getTitle(),
                            row.getDescription(),
                            parseTags(row.getTags()),
                            counts.getOrDefault("like", 0L),
                            commentCount,
                            counts.getOrDefault("fav", 0L),
                            row.getPublishTime(),
                            hotScore
                    );
                })
                .toList();

        // Sort if needed
        List<SearchPostItem> sorted = items;
        if ("hot".equals(sort)) {
            sorted = items.stream()
                    .sorted((a, b) -> Double.compare(b.hotScore() != null ? b.hotScore() : 0,
                            a.hotScore() != null ? a.hotScore() : 0))
                    .toList();
        } else if ("latest".equals(sort)) {
            sorted = items.stream()
                    .sorted((a, b) -> {
                        Instant ta = a.publishedAt() != null ? a.publishedAt() : Instant.EPOCH;
                        Instant tb = b.publishedAt() != null ? b.publishedAt() : Instant.EPOCH;
                        return tb.compareTo(ta);
                    })
                    .toList();
        }

        return new SearchPageResponse(sorted, boundedPage, boundedSize,
                sorted.size(),
                (int) Math.ceil((double) sorted.size() / boundedSize),
                sorted.size() >= boundedSize, sort);
    }

    @Transactional(readOnly = true)
    public AgentPostContext getPost(long postId) {
        KnowPostDetailRow row = knowPostMapper.findDetailById(postId);
        if (row == null || !"published".equals(row.getStatus()) || !"public".equals(row.getVisible())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "帖子不存在或不可公开读取");
        }
        String body = "";
        try {
            body = ossStorageService.readTextObject(row.getContentObjectKey(), 512 * 1024);
        } catch (Exception ignored) {}
        return new AgentPostContext(
                String.valueOf(row.getId()),
                row.getTitle(),
                row.getDescription(),
                body,
                parseTags(row.getTags()),
                row.getCreatorId() != null ? String.valueOf(row.getCreatorId()) : null,
                row.getAuthorNickname(),
                row.getPublishTime(),
                row.getContentOrigin()
        );
    }

    @Transactional
    public void deletePost(long userId, long postId) {
        knowPostService.delete(userId, postId);
    }

    @Transactional(readOnly = true)
    public List<AgentOwnPostSummary> getMyPosts(long userId, int page, int size) {
        int boundedSize = Math.min(Math.max(size, 1), 50);
        int boundedOffset = Math.max(page - 1, 0) * boundedSize;
        return knowPostMapper.listMyPublished(userId, boundedSize, boundedOffset)
                .stream()
                .map(row -> new AgentOwnPostSummary(
                        String.valueOf(row.getId()),
                        row.getTitle(),
                        row.getDescription(),
                        "published",
                        "public",
                        null,
                        row.getPublishTime(),
                        row.getPublishTime()
                ))
                .toList();
    }

    @Transactional
    public DraftResponse createDraft(long userId, AgentDraftCreateRequest request) {
        validateSummary(request.summary());
        // The Agent Runtime writes drafts directly through the canonical Agent
        // publication API; content arrives already generated by the host LLM.
        long id = knowPostService.createDraft(userId, "AI_ASSISTED");

        String objectKey = "knowposts/" + id + "/content.md";
        String etag = ossStorageService.putTextObject(objectKey, request.content(), "text/markdown");
        byte[] bytes = request.content().getBytes(java.nio.charset.StandardCharsets.UTF_8);
        String sha = sha256Hex(bytes);
        knowPostService.confirmContent(userId, id, objectKey, etag, (long) bytes.length, sha);

        List<String> tags = null;
        String visibility = request.visibility() != null ? request.visibility() : "public";
        String summary = request.summary() != null ? request.summary() : "";
        knowPostService.updateMetadata(userId, id, request.title(), null, tags, null, visibility, false, summary);

        return getDraft(userId, id);
    }

    @Transactional(readOnly = true)
    public DraftResponse getDraft(long userId, long draftId) {
        KnowPost post = knowPostMapper.findById(draftId);
        if (post == null || post.getCreatorId() == null || post.getCreatorId() != userId) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或不属于当前用户");
        }
        String content = "";
        try {
            content = ossStorageService.readTextObject(post.getContentObjectKey(), 512 * 1024);
        } catch (Exception ignored) {}
        return toDraftResponse(post, content);
    }

    @Transactional(readOnly = true)
    public List<DraftResponse> getMyDrafts(long userId) {
        return knowPostMapper.listOwnPostsForAgent(userId, 50, 0)
                .stream()
                .filter(p -> "draft".equals(p.getStatus()))
                .map(p -> toDraftResponse(p, ""))
                .toList();
    }

    @Transactional
    public DraftResponse updateDraft(long userId, long draftId, AgentDraftUpdateRequest request) {
        validateSummary(request.summary());
        KnowPost post = knowPostMapper.findById(draftId);
        if (post == null || post.getCreatorId() == null || post.getCreatorId() != userId) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或不属于当前用户");
        }
        if (!"draft".equals(post.getStatus())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "只能修改草稿状态的内容");
        }

        // Optimistic lock: if expectedVersion is provided, check it matches current updateTime
        if (request.expectedVersion() != null) {
            Instant current = post.getUpdateTime();
            if (current == null || !current.equals(request.expectedVersion())) {
                throw new BusinessException(ErrorCode.CONFLICT,
                        "DRAFT_VERSION_CONFLICT: 草稿已被修改，请重新获取最新版本后重试。" +
                        "当前版本: " + (current != null ? current.toString() : "null"));
            }
        }

        if (request.content() != null) {
            String objectKey = post.getContentObjectKey() != null ? post.getContentObjectKey()
                    : "knowposts/" + draftId + "/content.md";
            String etag = ossStorageService.putTextObject(objectKey, request.content(), "text/markdown");
            byte[] bytes = request.content().getBytes(java.nio.charset.StandardCharsets.UTF_8);
            String sha = sha256Hex(bytes);
            knowPostService.confirmContent(userId, draftId, objectKey, etag, (long) bytes.length, sha);
        }

        String title = request.title() != null ? request.title() : post.getTitle();
        String summary = request.summary() != null ? request.summary() : post.getDescription();
        String visibility = request.visibility() != null ? request.visibility() : post.getVisible();
        List<String> tags = request.tags() != null ? request.tags() : parseTags(post.getTags());

        knowPostService.updateMetadata(userId, draftId, title, null, tags, null, visibility, false, summary);
        return getDraft(userId, draftId);
    }

    /**
     * Soft-delete one draft through the Agent Facade.
     *
     * A pending publication remains a separate business resource.  Deleting
     * its draft would otherwise make the scheduler fail later, so callers
     * must cancel the schedule explicitly before deleting the draft.
     */
    @Transactional
    public void deleteDraft(long userId, long draftId) {
        KnowPost post = knowPostMapper.findByIdForUpdate(draftId);
        if (post == null || post.getCreatorId() == null || post.getCreatorId() != userId) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "Draft does not exist or is not owned by current user");
        }
        if ("deleted".equals(post.getStatus())) {
            return;
        }
        if (!"draft".equals(post.getStatus())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "Only drafts may be deleted through the Agent Facade");
        }
        if (scheduledPublicationMapper.countActiveByUserAndDraft(userId, draftId) > 0) {
            throw new BusinessException(ErrorCode.BAD_REQUEST,
                    "Cancel the active publication schedule before deleting this draft");
        }
        knowPostService.delete(userId, draftId);
    }

    private static void validateSummary(String summary) {
        if (summary != null
                && summary.codePointCount(0, summary.length())
                > DraftMetadataContract.DESCRIPTION_MAX_LENGTH) {
            throw new BusinessException(
                    ErrorCode.FIELD_TOO_LONG,
                    "field=summary; maxLength=" + DraftMetadataContract.DESCRIPTION_MAX_LENGTH
                            + "; actualLength=" + summary.codePointCount(0, summary.length()));
        }
    }

    @Transactional(readOnly = true)
    public AgentCommentPageResponse getPostComments(long postId, String cursor, int size) {
        KnowPostDetailRow row = knowPostMapper.findDetailById(postId);
        if (row == null || !"published".equals(row.getStatus()) || !"public".equals(row.getVisible())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "帖子不存在或不可公开读取");
        }
        Long cursorVal = null;
        if (cursor != null && !cursor.isBlank()) {
            try { cursorVal = Long.parseLong(cursor); } catch (NumberFormatException ignored) {}
        }
        CommentPageResponse page = commentService.list(postId, null, cursorVal, size, null);
        List<AgentCommentResponse> items = page.items().stream()
                .map(this::toAgentComment)
                .toList();
        return new AgentCommentPageResponse(items, page.nextCursor(), page.hasMore());
    }

    @Transactional
    public AgentCommentResponse replyToComment(long userId, String postIdStr, String parentCommentIdStr, String content) {
        long postId = parseLong(postIdStr);
        long parentCommentId = parseLong(parentCommentIdStr);
        CommentResponse created = commentService.create(userId,
                new CommentCreateRequest(postId, parentCommentId, content));
        return toAgentComment(created);
    }

    @Transactional(readOnly = true)
    public AgentCommentResponse getComment(long userId, String commentIdStr) {
        CommentResponse comment = commentService.get(parseLong(commentIdStr), userId);
        return toAgentComment(comment);
    }

    @Transactional(readOnly = true)
    public PostAnalyticsResponse getPostAnalytics(long postId) {
        KnowPostDetailRow row = knowPostMapper.findDetailById(postId);
        if (row == null || !"published".equals(row.getStatus()) || !"public".equals(row.getVisible())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "帖子不存在或不可公开读取");
        }
        Map<String, Long> counts = counterService.getCounts("knowpost",
                String.valueOf(postId), List.of("like", "fav"));
        long commentCount = 0;
        try { commentCount = commentService.list(postId, null, null, 0, null).items().size(); } catch (Exception ignored) {}
        return new PostAnalyticsResponse(
                String.valueOf(postId),
                counts.getOrDefault("like", 0L),
                commentCount,
                counts.getOrDefault("fav", 0L),
                0L,
                0L
        );
    }

    @Transactional(readOnly = true)
    public UserAnalyticsSummaryResponse getMyAnalyticsSummary(long userId) {
        long totalPublished = knowPostMapper.countMyPublished(userId);
        long followerCount = 0, followingCount = 0;
        try {
            followingCount = relationMapper.countFollowingActive(userId);
            followerCount = relationMapper.countFollowerActive(userId);
        } catch (Exception ignored) {}

        return new UserAnalyticsSummaryResponse(
                totalPublished,
                0L, 0L, 0L,
                followerCount,
                followingCount
        );
    }

    private DraftResponse toDraftResponse(KnowPost post, String content) {
        Instant ut = post.getUpdateTime();
        return new DraftResponse(
                String.valueOf(post.getId()),
                post.getCreatorId() != null ? String.valueOf(post.getCreatorId()) : null,
                post.getTitle(),
                content,
                post.getDescription(),
                parseTags(post.getTags()),
                post.getVisible(),
                ut != null ? (int) ut.getEpochSecond() : null,
                post.getStatus(),
                post.getContentOrigin(),
                post.getCreateTime(),
                ut
        );
    }

    private AgentCommentResponse toAgentComment(CommentResponse c) {
        return new AgentCommentResponse(
                c.id(), c.postId(), c.parentId(), c.rootId(),
                c.userId(), c.authorNickname(), c.authorAvatar(),
                c.content(), c.top(), c.replyCount(),
                c.likeCount(), c.assistant(), c.createTime()
        );
    }

    private List<String> parseTags(String tagsJson) {
        if (tagsJson == null || tagsJson.isBlank()) return List.of();
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().readValue(tagsJson,
                    new com.fasterxml.jackson.core.type.TypeReference<List<String>>() {});
        } catch (Exception e) {
            return List.of();
        }
    }

    private long parseLong(String value) {
        try { return Long.parseLong(value); }
        catch (NumberFormatException e) { throw new BusinessException(ErrorCode.BAD_REQUEST, "ID格式不正确"); }
    }

    private static String sha256Hex(byte[] bytes) {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(bytes);
            StringBuilder sb = new StringBuilder(digest.length * 2);
            for (byte b : digest) sb.append(String.format("%02x", b));
            return sb.toString();
        } catch (Exception e) { return ""; }
    }

    // ── Lightweight search query tokenization (zero-dependency) ────────
    // LLM-generated queries are usually space-separated phrases
    // ("Java 后端 面试"); we normalize punctuation/width, split on
    // whitespace and CJK↔Latin boundaries, and drop stop words so the
    // token-aware SQL can OR-recall each meaningful term.

    /** Stop words that add no retrieval value (conservative set). */
    private static final Set<String> SEARCH_STOP_WORDS = Set.of(
            "的", "了", "在", "是", "和", "与", "及", "或", "比较", "相对", "最近", "最新",
            "热门", "一些", "这些", "那些", "什么", "怎么", "如何", "需要", "帮忙", "帮我",
            "请", "请问", "一下", "一个", "那个", "这个", "关于",
            "搜索", "查找", "查询", "搜", "找", "写", "看看", "浏览",
            "the", "a", "an", "of", "for", "to", "in", "on", "about", "with",
            "how", "what", "please", "help"
    );

    /** CJK↔Latin boundary splitter: "java后端" -> "java" + "后端". */
    private static final Pattern CJK_LATIN_BOUNDARY = Pattern.compile(
            "(?<=[a-z0-9])(?=[\\u4e00-\\u9fff])|(?<=[\\u4e00-\\u9fff])(?=[a-z0-9])");

    static List<String> tokenizeSearchQuery(String query) {
        if (query == null || query.isBlank()) return List.of();
        String normalized = toHalfWidth(query).toLowerCase(Locale.ROOT)
                .replaceAll("[\\p{P}\\p{S}\\s]+", " ");
        LinkedHashSet<String> tokens = new LinkedHashSet<>();
        for (String part : normalized.split(" ")) {
            if (part.isEmpty()) continue;
            for (String segment : CJK_LATIN_BOUNDARY.split(part)) {
                if (segment.isEmpty()) continue;
                if (SEARCH_STOP_WORDS.contains(segment)) continue;
                // Strip embedded multi-char stop words ("搜索最近比较热门的"
                // -> ""), but never single CJK chars ("的" in "目的"/"的确").
                segment = stripEmbeddedStopWords(segment);
                for (String sub : segment.split(" ")) {
                    if (sub.isEmpty() || SEARCH_STOP_WORDS.contains(sub)) continue;
                    if (sub.length() == 1 && isSingleCjk(sub.charAt(0))) continue;
                    tokens.add(sub);
                }
            }
        }
        return new ArrayList<>(tokens);
    }

    private static String stripEmbeddedStopWords(String segment) {
        String cleaned = segment;
        for (String stop : SEARCH_STOP_WORDS) {
            if (stop.length() >= 2) {
                cleaned = cleaned.replace(stop, " ");
            }
        }
        return cleaned;
    }

    private static boolean isSingleCjk(char c) {
        return c >= '\u4e00' && c <= '\u9fff';
    }

    /** Full-width (CJK punctuation/ASCII variants) → half-width. */
    private static String toHalfWidth(String value) {
        StringBuilder sb = new StringBuilder(value.length());
        for (char c : value.toCharArray()) {
            if (c == '\u3000') {
                sb.append(' ');
            } else if (c >= '\uFF01' && c <= '\uFF5E') {
                sb.append((char) (c - 0xFEE0));
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }
}
