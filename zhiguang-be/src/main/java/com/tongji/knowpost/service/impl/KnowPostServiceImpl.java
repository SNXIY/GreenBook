package com.tongji.knowpost.service.impl;

import com.fasterxml.jackson.core.type.TypeReference;
import com.tongji.counter.service.UserCounterService;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.knowpost.service.FeedIndexService;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.common.trace.TraceContextFilter;
import com.tongji.knowpost.id.SnowflakeIdGenerator;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.knowpost.api.dto.KnowPostDetailResponse;
import com.tongji.knowpost.api.dto.PostTaskItemResponse;
import com.tongji.knowpost.api.dto.PublishStatusResponse;
import com.github.benmanes.caffeine.cache.Cache;
import com.tongji.counter.service.CounterService;
import com.tongji.moderation.ModerationAgentClient;
import com.tongji.moderation.ModerationReasonPresenter;
import com.tongji.storage.OssStorageService;
import com.tongji.cache.hotkey.HotKeyDetector;
import jakarta.annotation.Resource;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import java.time.Duration;
import java.time.Instant;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ThreadLocalRandom;

@Service
public class KnowPostServiceImpl implements KnowPostService {

    private final KnowPostMapper mapper;
    @Resource
    private final SnowflakeIdGenerator idGen;
    private final ObjectMapper objectMapper;
    private final CounterService counterService;
    private final UserCounterService userCounterService;
    private final StringRedisTemplate redis;
    @Qualifier("knowPostDetailCache")
    private final Cache<String, KnowPostDetailResponse> knowPostDetailCache;
    private final HotKeyDetector hotKey;
    private static final Logger log = LoggerFactory.getLogger(KnowPostServiceImpl.class);
    private static final int DETAIL_LAYOUT_VER = 1;
    private final ConcurrentHashMap<String, Object> singleFlight = new ConcurrentHashMap<>();
    private final FeedIndexService feedIndexService;
    private final ModerationAgentClient moderationAgentClient;
    private final OssStorageService ossStorageService;
    private final ThreadPoolTaskExecutor taskExecutor;

    // 手动编写构造器，Spring的@Qualifier直接标注在参数上（核心）
    public KnowPostServiceImpl(
            KnowPostMapper mapper,
            SnowflakeIdGenerator idGen,
            ObjectMapper objectMapper,
            CounterService counterService,
            UserCounterService userCounterService,
            StringRedisTemplate redis,
            @Qualifier("knowPostDetailCache") Cache<String, KnowPostDetailResponse> knowPostDetailCache,
            HotKeyDetector hotKey,
            FeedIndexService feedIndexService,
            ModerationAgentClient moderationAgentClient,
            OssStorageService ossStorageService,
            @Qualifier("taskExecutor") ThreadPoolTaskExecutor taskExecutor
    ) {
        this.mapper = mapper;
        this.idGen = idGen;
        this.objectMapper = objectMapper;
        this.counterService = counterService;
        this.userCounterService = userCounterService;
        this.redis = redis;
        this.knowPostDetailCache = knowPostDetailCache;
        this.hotKey = hotKey;
        this.feedIndexService = feedIndexService;
        this.moderationAgentClient = moderationAgentClient;
        this.ossStorageService = ossStorageService;
        this.taskExecutor = taskExecutor;
    }
    @Transactional
    public long createDraft(long creatorId) {
        return createDraft(creatorId, "MANUAL");
    }

    @Transactional
    public long createDraft(long creatorId, String contentOrigin) {
        long id = idGen.nextId();
        Instant now = Instant.now();
        String origin = (contentOrigin == null || contentOrigin.isBlank()) ? "MANUAL" : contentOrigin;
        KnowPost post = KnowPost.builder()
                .id(id)
                .creatorId(creatorId)
                .status("draft")
                .type("image_text")
                .visible("public")
                .isTop(false)
                .contentOrigin(origin)
                .createTime(now)
                .updateTime(now)
                .build();
        mapper.insertDraft(post);
        return id;
    }

    /**
     * 接收创作 Agent handoff：创建 AI_ASSISTED 草稿并写入正文。
     */
    @Transactional
    public long createAiDraft(long creatorId, String title, String bodyMarkdown, String description, String contentSha256) {
        long id = createDraft(creatorId, "AI_ASSISTED");
        String objectKey = "knowposts/" + id + "/content.md";
        String etag = ossStorageService.putTextObject(objectKey, bodyMarkdown, "text/markdown");
        byte[] bytes = bodyMarkdown == null ? new byte[0] : bodyMarkdown.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        String sha = (contentSha256 == null || contentSha256.isBlank())
                ? sha256Hex(bytes)
                : contentSha256;
        confirmContent(creatorId, id, objectKey, etag, (long) bytes.length, sha);
        String desc = description;
        if (desc == null || desc.isBlank()) {
            desc = bodyMarkdown == null ? "" : bodyMarkdown.replaceAll("\\s+", " ").trim();
            if (desc.length() > 50) {
                desc = desc.substring(0, 50);
            }
        }
        updateMetadata(creatorId, id, title, null, null, null, "public", false, desc);
        return id;
    }

    private static String sha256Hex(byte[] bytes) {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(bytes);
            StringBuilder sb = new StringBuilder(digest.length * 2);
            for (byte b : digest) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * 确认内容上传（写入 objectKey、etag、大小、校验和，并生成公共 URL）。
     */
    @Transactional
    public void confirmContent(long creatorId, long id, String objectKey, String etag, Long size, String sha256) {
        // 缓存双删
        invalidateCache(id);

        KnowPost post = KnowPost.builder()
                .id(id)
                .creatorId(creatorId)
                .contentObjectKey(objectKey)
                .contentEtag(etag)
                .contentSize(size)
                .contentSha256(sha256)
                .contentUrl(ossStorageService.publicObjectUrl(objectKey))
                .updateTime(Instant.now())
                .build();

        int updated = mapper.updateContent(post);
        if (updated == 0) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }

        invalidateCache(id);

    }

    /**
     * 更新元数据：标题、标签、可见性、置顶、图片列表等。
     */
    @Transactional
    public void updateMetadata(long creatorId, long id, String title, Long tagId, List<String> tags, List<String> imgUrls, String visible, Boolean isTop, String description) {
        invalidateCache(id);

        KnowPost post = KnowPost.builder()
                .id(id)
                .creatorId(creatorId)
                .title(title)
                .tagId(tagId)
                .tags(toJsonOrNull(tags))
                .imgUrls(toJsonOrNull(imgUrls))
                .visible(visible)
                .isTop(isTop)
                .description(description)
                .type("image_text")
                .updateTime(Instant.now())
                .build();

        int updated = mapper.updateMetadata(post);

        if (updated == 0) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }

        invalidateCache(id);
    }

    /**
     * 发布草稿。人工帖进入 reviewing 并送审 moderation-agent；AI_ASSISTED 跳过外部审核直接发布。
     * @return reviewing | published
     */
    @Transactional
    public String publish(long creatorId, long id) {
        KnowPost post = mapper.findById(id);
        if (post == null || post.getCreatorId() == null || !post.getCreatorId().equals(creatorId)) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }

        boolean aiAssisted = "AI_ASSISTED".equalsIgnoreCase(post.getContentOrigin());
        if (aiAssisted) {
            int updated = mapper.publish(id, creatorId);
            if (updated == 0) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
            }
            afterPublished(id, creatorId);
            return "published";
        }

        // 人工创作：一律进入审核（不再仅依赖敏感词命中）
        int reviewingUpdated = mapper.markReviewing(id, creatorId);
        if (reviewingUpdated == 0) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }
        submitReviewAfterCommit(id, creatorId);
        return "reviewing";
    }

    @Override
    public PublishStatusResponse getPublishStatus(long creatorId, long id) {
        KnowPost post = mapper.findById(id);
        if (post == null || post.getCreatorId() == null || !post.getCreatorId().equals(creatorId)) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "内容不存在或无权限");
        }
        return new PublishStatusResponse(
                String.valueOf(id),
                post.getStatus(),
                post.getModerationTaskId(),
                ModerationReasonPresenter.forUser(post.getModerationReason())
        );
    }

    @Override
    public List<PostTaskItemResponse> listTaskItems(long creatorId, int limit) {
        int boundedLimit = Math.min(Math.max(limit, 1), 50);
        return mapper.listOwnPostsForAssistant(creatorId, boundedLimit, 0)
                .stream()
                .map(post -> new PostTaskItemResponse(
                        String.valueOf(post.getId()),
                        post.getTitle(),
                        post.getStatus(),
                        post.getContentOrigin(),
                        post.getModerationTaskId(),
                        post.getModerationReason() == null || post.getModerationReason().isBlank()
                                ? null
                                : ModerationReasonPresenter.forUser(post.getModerationReason()),
                        post.getCreateTime(),
                        post.getUpdateTime(),
                        post.getPublishTime()
                ))
                .toList();
    }

    @Override
    public String getContent(long creatorId, long id) {
        KnowPost post = mapper.findById(id);
        if (post == null || post.getCreatorId() == null || !post.getCreatorId().equals(creatorId)) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }
        return ossStorageService.readTextObject(post.getContentObjectKey(), 512 * 1024);
    }

    private void submitReviewAfterCommit(long id, long creatorId) {
        String traceId = TraceContextFilter.currentOrCreate();
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    submitAiReview(id, creatorId, traceId);
                }
            });
        } else {
            submitAiReview(id, creatorId, traceId);
        }
    }

    private void submitAiReview(long id, long creatorId, String traceId) {
        CompletableFuture.runAsync(
                        () -> submitOrReconcileReview(id, creatorId, traceId),
                        taskExecutor
                )
                .exceptionally(ex -> {
                    log.warn("AI moderation task failed, post={}, error={}", id, ex.getMessage());
                    return null;
                });
    }

    private void submitOrReconcileReview(long id, long creatorId, String traceId) {
        KnowPost post = mapper.findById(id);
        if (post == null || !"reviewing".equals(post.getStatus())) {
            return;
        }

        if (post.getModerationTaskId() != null && !post.getModerationTaskId().isBlank()) {
            try {
                ModerationAgentClient.ModerationAgentDecision decision =
                        moderationAgentClient.getTask(post.getModerationTaskId());
                applyModerationDecision(post, decision);
            } catch (Exception ex) {
                log.warn("Moderation reconciliation failed for post={}: {}", id, ex.getMessage());
            }
            return;
        }

        String content = ossStorageService.readTextObject(post.getContentObjectKey(), 256 * 1024);
        String reviewFingerprint = sha256Hex(String.join("\n",
                post.getTitle() == null ? "" : post.getTitle(),
                post.getDescription() == null ? "" : post.getDescription(),
                post.getTags() == null ? "" : post.getTags(),
                content
        ).getBytes(java.nio.charset.StandardCharsets.UTF_8));
        String idempotencyKey = "knowpost:" + id + ":publish:" + reviewFingerprint.substring(0, 24);
        try {
            String taskId = moderationAgentClient.submitReview(
                    post.getTitle(),
                    post.getDescription(),
                    post.getTags(),
                    content,
                    String.valueOf(id),
                    String.valueOf(creatorId),
                    idempotencyKey,
                    traceId
            );
            mapper.updateModerationTaskId(id, creatorId, taskId, "审核任务已提交");
            log.info("Post submitted for asynchronous AI moderation, post={}, task={}", id, taskId);
        } catch (Exception ex) {
            log.warn("Moderation agent unavailable for post={}, keep reviewing: {}", id, ex.getMessage());
        }
    }

    private void applyModerationDecision(
            KnowPost post,
            ModerationAgentClient.ModerationAgentDecision decision
    ) {
        if (post == null || post.getId() == null || post.getCreatorId() == null
                || !"reviewing".equals(post.getStatus())) {
            return;
        }
        long id = post.getId();
        long creatorId = post.getCreatorId();
        mapper.updateModerationTaskId(id, creatorId, decision.taskId(), decision.reason());

        if (decision.pass()) {
            int updated = mapper.publish(id, creatorId);
            if (updated == 0) {
                log.warn("Post publish after AI moderation skipped, post={}", id);
                return;
            }
            afterPublished(id, creatorId);
            log.info("Post passed AI moderation and published, post={}", id);
            return;
        }

        if (decision.reject()) {
            mapper.markRejected(id, creatorId);
            log.info("Post rejected by AI moderation, post={}, reason={}", id, decision.reason());
            return;
        }

        // WAITING_REVIEW / RUNNING / FAILED：保持 reviewing，由回调或定时任务对账
        log.info("Post remains reviewing, post={}, status={}, action={}",
                id, decision.status(), decision.finalAction());
    }

    private void afterPublished(long id, long creatorId) {
        try {
            userCounterService.incrementPosts(creatorId, 1);
        } catch (Exception ignored) {}

        try {
            feedIndexService.indexPublishedPost(id);
        } catch (Exception e) {
            log.warn("Feed index after publish failed, post {}: {}", id, e.getMessage());
        }

    }

    @Scheduled(fixedDelayString = "${moderation.reviewing-scan-delay-ms:300000}",
            initialDelayString = "${moderation.reviewing-scan-initial-delay-ms:60000}")
    public void resubmitStaleReviewingPosts() {
        List<KnowPost> posts = mapper.listReviewingBefore(5, 20);
        for (KnowPost post : posts) {
            if (post.getId() != null && post.getCreatorId() != null) {
                submitAiReview(
                        post.getId(),
                        post.getCreatorId(),
                        "moderation-reconcile-" + post.getId()
                );
            }
        }
    }

    @Override
    public void resumeAfterModerationTask(String moderationTaskId) {
        if (moderationTaskId == null || moderationTaskId.isBlank()) {
            return;
        }
        KnowPost post = mapper.findByModerationTaskId(moderationTaskId);
        if (post == null || post.getId() == null || post.getCreatorId() == null
                || !"reviewing".equals(post.getStatus())) {
            return;
        }
        submitAiReview(
                post.getId(),
                post.getCreatorId(),
                "moderation-resume-" + moderationTaskId
        );
    }

    @Override
    @Transactional
    public void applyModerationResult(
            String moderationTaskId,
            String contentId,
            String status,
            String finalAction,
            String reason
    ) {
        if (moderationTaskId == null || moderationTaskId.isBlank()) {
            return;
        }
        KnowPost post = mapper.findByModerationTaskId(moderationTaskId);
        if (post == null && contentId != null && contentId.matches("\\d+")) {
            post = mapper.findById(Long.parseLong(contentId));
            if (post != null && post.getId() != null && post.getCreatorId() != null
                    && "reviewing".equals(post.getStatus())) {
                mapper.updateModerationTaskId(
                        post.getId(), post.getCreatorId(), moderationTaskId, reason
                );
            }
        }
        if (post == null || post.getId() == null || post.getCreatorId() == null
                || !"reviewing".equals(post.getStatus())) {
            return;
        }
        ModerationAgentClient.ModerationAgentDecision decision =
                new ModerationAgentClient.ModerationAgentDecision(
                        moderationTaskId,
                        status == null ? "" : status.toUpperCase(Locale.ROOT),
                        finalAction == null ? "" : finalAction.toUpperCase(Locale.ROOT),
                        reason == null ? "" : reason
                );
        applyModerationDecision(post, decision);
    }

    @Transactional
    public void updateTop(long creatorId, long id, boolean isTop) {
        invalidateCache(id);

        int updated = mapper.updateTop(id, creatorId, isTop);

        if (updated == 0) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }

        invalidateCache(id);
    }

    /**
     * 设置可见性（权限）。
     */
    @Transactional
    public void updateVisibility(long creatorId, long id, String visible) {
        if (!isValidVisible(visible)) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "可见性取值非法");
        }

        invalidateCache(id);

        int updated = mapper.updateVisibility(id, creatorId, visible);

        if (updated == 0) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }

        invalidateCache(id);
    }

    /**
     * 软删除。
     */
    @Transactional
    public void delete(long creatorId, long id) {
        KnowPost existing = mapper.findById(id);
        if (existing == null || existing.getCreatorId() == null
                || existing.getCreatorId() != creatorId) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "帖子不存在或无权限");
        }
        if ("deleted".equals(existing.getStatus())) {
            return;
        }
        invalidateCache(id);

        int updated = mapper.softDelete(id, creatorId);
        if (updated == 0) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }

        invalidateCache(id);
        feedIndexService.invalidateFeedCaches();
        if ("published".equals(existing.getStatus())) {
            try {
                userCounterService.incrementPosts(creatorId, -1);
            } catch (Exception ignored) {}
        }
    }

    private boolean isValidVisible(String visible) {
        if (visible == null) {
            return false;
        }

        return switch (visible) {
            case "public", "followers", "school", "private", "unlisted" -> true;
            default -> false;
        };
    }

    private String toJsonOrNull(List<String> list) {
        if (list == null) {
            return null;
        }

        try {
            return objectMapper.writeValueAsString(list);
        } catch (JsonProcessingException e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "JSON 处理失败");
        }
    }

    /**
     * 获取知文详情（含作者信息、图片列表）。
     * <p>
     * 流程：
     * 1. 尝试读取 Redis 缓存。
     * 2. 若缓存命中，直接返回（需叠加实时计数与用户状态）。
     * 3. 若缓存未命中，使用 SingleFlight 锁机制防止缓存击穿。
     * 4. 锁内再次检查缓存（双重检查）。
     * 5. 若仍未命中，回源查询数据库。
     * 6. 校验内容状态与访问权限。
     * 7. 组装数据并写入 Redis 缓存（带随机过期时间与热点自动延期）。
     * 8. 返回最终结果（叠加用户维度状态）。
     * </p>
     *
     * @param id 知文 ID
     * @param currentUserIdNullable 当前用户 ID（可空，用于判断权限与点赞状态）
     * @return 知文详情响应
     */
    @Transactional(readOnly = true)
    public KnowPostDetailResponse getDetail(long id, Long currentUserIdNullable) {
        // 1. 构造缓存 Key：knowpost:detail:{id}:v{version}
        String pageKey = "knowpost:detail:" + id + ":v" + DETAIL_LAYOUT_VER;
        
        // 0. L1 本地缓存（Caffeine）
        KnowPostDetailResponse local = knowPostDetailCache.getIfPresent(pageKey);
        if (local != null) {
            recordHotKeyAndExtendTtl(id, pageKey);
            log.info("detail source=local key={}", pageKey);
            return enrichDetailResponse(local, currentUserIdNullable, true);
        }

        String cached = redis.opsForValue().get(pageKey);

        // 2. 第一次尝试处理缓存命中
        // 如果缓存中有数据（且不是 "NULL"），则解析并返回
        KnowPostDetailResponse resp = tryProcessCacheHit(cached, id, pageKey, currentUserIdNullable, "page");
        if (resp != null) {
            return resp;
        }

        // 3. 缓存未命中，进入 SingleFlight 模式
        // 对同一个 pageKey 加锁，防止高并发下大量请求同时打到数据库（缓存击穿/惊群效应）
        Object lock = singleFlight.computeIfAbsent(pageKey, k -> new Object());
        synchronized (lock) {
            // 4. 双重检查（Double Check）
            // 在获取锁后，再次检查缓存，因为在排队等待锁的过程中，前一个请求可能已经把数据写入缓存了
            String again = redis.opsForValue().get(pageKey);
            try {
                resp = tryProcessCacheHit(again, id, pageKey, currentUserIdNullable, "page(after-flight)");
            } catch (BusinessException e) {
                // 如果缓存中明确记录了 "NULL"（即内容不存在），则直接抛出异常，不再查库
                singleFlight.remove(pageKey);
                throw e;
            }
            if (resp != null) {
                // 缓存已由其他线程填充，直接返回
                singleFlight.remove(pageKey);
                return resp;
            }

            // 5. 数据库回源查询
            KnowPostDetailRow row = mapper.findDetailById(id);
            
            // 6. 处理内容不存在或已删除的情况
            // 写入 "NULL" 空值缓存，防止缓存穿透（查询不存在的数据导致一直打数据库）
            if (row == null || "deleted".equals(row.getStatus())) {
                redis.opsForValue().set(pageKey, "NULL", java.time.Duration.ofSeconds(30 + java.util.concurrent.ThreadLocalRandom.current().nextInt(31)));
                singleFlight.remove(pageKey);
                throw new BusinessException(ErrorCode.BAD_REQUEST, "内容不存在");
            }

            // 7. 权限校验
            // 公开策略：状态为 published 且可见性为 public 的内容可直接访问
            // 私有策略：否则仅作者本人可见
            boolean isPublic = "published".equals(row.getStatus()) && "public".equals(row.getVisible());
            boolean isOwner = currentUserIdNullable != null && row.getCreatorId() != null && currentUserIdNullable.equals(row.getCreatorId());
            if (!isPublic && !isOwner) {
                singleFlight.remove(pageKey);
                throw new BusinessException(ErrorCode.BAD_REQUEST, "无权限查看");
            }

            // 8. 组装响应对象
            // 解析图片和标签 JSON
            List<String> images = parseStringArray(row.getImgUrls());
            List<String> tags = parseStringArray(row.getTags());
            
            // 此处查询的计数仅作为缓存的基础值，后续 enrich 会刷新
            Map<String, Long> counts = counterService.getCounts("knowpost", String.valueOf(row.getId()), List.of("like", "fav"));
            Long likeCount = counts.getOrDefault("like", 0L);
            Long favoriteCount = counts.getOrDefault("fav", 0L);

            resp = new KnowPostDetailResponse(
                    String.valueOf(row.getId()),
                    row.getTitle(),
                    row.getDescription(),
                    row.getContentUrl(),
                    images,
                    tags,
                    String.valueOf(row.getCreatorId()),
                    row.getAuthorAvatar(),
                    row.getAuthorNickname(),
                    row.getAuthorTagJson(),
                    likeCount,
                    favoriteCount,
                    null, // liked 状态暂时留空，由 enrich 填充
                    null, // faved 状态暂时留空，由 enrich 填充
                    row.getIsTop(),
                    row.getVisible(),
                    row.getType(),
                    row.getPublishTime(),
                    row.getContentOrigin(),
                    row.getStatus()
            );

            // 9. 写入 Redis 缓存
            try {
                String json = objectMapper.writeValueAsString(resp);
                int baseTtl = 60;
                // 增加随机抖动（Jitter），防止大量缓存同时过期（雪崩）
                int jitter = ThreadLocalRandom.current().nextInt(30);
                // 根据热度检测结果动态调整 TTL，热点内容缓存时间更长
                int target = hotKey.ttlForPublic(baseTtl, pageKey);
                redis.opsForValue().set(pageKey, json, Duration.ofSeconds(Math.max(target, baseTtl + jitter)));

                // L1 填充
                knowPostDetailCache.put(pageKey, resp);

                log.info("detail source=db key={}", pageKey);
            } catch (Exception ignored) {}

            // 10. 释放锁并返回最终结果
            // 返回前调用 enrich 填充用户维度的 liked/faved 状态
            singleFlight.remove(pageKey);
            return enrichDetailResponse(resp, currentUserIdNullable, false);
        }
    }

    /**
     * 尝试处理缓存命中逻辑。
     *
     * @param cached Redis 中读取的缓存字符串
     * @param id 内容 ID
     * @param pageKey 页面缓存 Key
     * @param uid 当前用户 ID
     * @param sourceLog 日志来源标识
     * @return 若成功处理命中则返回响应对象，否则返回 null
     */
    private KnowPostDetailResponse tryProcessCacheHit(String cached, long id, String pageKey, Long uid, String sourceLog) {
        // 1. 缓存为空，未命中
        if (cached == null) {
            return null;
        }
        
        // 2. 命中空值缓存（防止穿透）
        if ("NULL".equals(cached)) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "内容不存在");
        }
        
        try {
            // 3. 反序列化缓存数据
            KnowPostDetailResponse base = objectMapper.readValue(cached, KnowPostDetailResponse.class);

            // L1 填充
            knowPostDetailCache.put(pageKey, base);
            
            // 4. 记录热度并尝试续期
            // 如果该内容正在被高频访问，自动延长其缓存 TTL
            recordHotKeyAndExtendTtl(id, pageKey);
            log.info("detail source={} key={}", sourceLog, pageKey);
            
            // 5. 叠加实时数据（计数与用户状态）并返回
            return enrichDetailResponse(base, uid, true);
        } catch (Exception ignored) {
            // 反序列化失败等异常情况，视为未命中，回源修复
            return null;
        }
    }

    /**
     * 丰富详情响应：叠加实时计数与用户状态。
     *
     * @param base 基础响应对象（来自缓存或 DB）
     * @param uid 当前用户 ID
     * @param refreshCounts 是否需要从 CounterService 刷新计数（缓存命中时需要，DB 回源时不需要）
     * @return 叠加了最新状态的响应对象
     */
    private KnowPostDetailResponse enrichDetailResponse(KnowPostDetailResponse base, Long uid, boolean refreshCounts) {
        Long likeCount = base.likeCount();
        Long favoriteCount = base.favoriteCount();

        // 1. 刷新计数（仅在走缓存时执行）
        // 因为缓存中的计数可能是旧的，权威计数在 CounterService (Redis SDS)
        if (refreshCounts) {
            Map<String, Long> counts = counterService.getCounts("knowpost", base.id(), List.of("like", "fav"));
            if (counts != null) {
                likeCount = counts.getOrDefault("like", likeCount == null ? 0L : likeCount);
                favoriteCount = counts.getOrDefault("fav", favoriteCount == null ? 0L : favoriteCount);
            }
        }

        // 2. 获取用户维度的状态（是否已点赞/收藏）
        // 这部分数据是个性化的，不能存入公共缓存
        Boolean liked = uid != null && counterService.isLiked("knowpost", base.id(), uid);
        Boolean faved = uid != null && counterService.isFaved("knowpost", base.id(), uid);

        // 3. 构造新的 Record 对象返回
        return new KnowPostDetailResponse(
                base.id(),
                base.title(),
                base.description(),
                base.contentUrl(),
                base.images(),
                base.tags(),
                base.authorId(),
                base.authorAvatar(),
                base.authorNickname(),
                base.authorTagJson(),
                likeCount,
                favoriteCount,
                liked,
                faved,
                base.isTop(),
                base.visible(),
                base.type(),
                base.publishTime(),
                base.contentOrigin(),
                base.status()
        );
    }

    /**
     * 记录内容热度，并根据热度等级延长相关缓存的 TTL。
     * 延长的缓存包括：
     * 1. 详情页整页缓存 (knowpost:detail:{id})
     * 2. Feed 流内容片段缓存 (feed:item:{id})
     * 这样可以确保热点内容在 Feed 流中也不会轻易过期，避免 Feed 流回源。
     * @param id 内容 ID
     * @param detailPageKey 详情页缓存 Key
     */
    private void recordHotKeyAndExtendTtl(long id, String detailPageKey) {
        // 统一使用 knowpost:{id} 作为热度统计 Key
        String hotKeyId = "knowpost:" + id;
        hotKey.record(hotKeyId);
        
        int baseTtl = 60;
        int target = hotKey.ttlForPublic(baseTtl, hotKeyId);
        
        // 1. 延长详情页缓存
        Long detailTtl = redis.getExpire(detailPageKey);
        if (detailTtl < target) {
            redis.expire(detailPageKey, java.time.Duration.ofSeconds(target));
        }
        
        // 2. 延长 Feed 流内容片段缓存
        String itemKey = "feed:item:" + id;
        Long itemTtl = redis.getExpire(itemKey);
        if (itemTtl < target) {
            redis.expire(itemKey, java.time.Duration.ofSeconds(target));
        }
    }

    private void invalidateCache(long id) {
        String pageKey = "knowpost:detail:" + id + ":v" + DETAIL_LAYOUT_VER;

        redis.delete(pageKey);

        knowPostDetailCache.invalidate(pageKey);//使指定键的缓存条目立即失效
    }

    private List<String> parseStringArray(String json) {
        if (json == null || json.isBlank()) return Collections.emptyList();
        try {
            return objectMapper.readValue(json, new TypeReference<>() {
            });
        } catch (Exception e) {
            return Collections.emptyList();
        }
    }
}
