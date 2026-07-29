package com.tongji.knowpost.service.impl;

import com.tongji.knowpost.service.KnowPostFeedService;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.knowpost.api.dto.FeedItemResponse;
import com.tongji.knowpost.api.dto.FeedPageResponse;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPostFeedRow;
import com.tongji.counter.service.CounterService;
import com.tongji.relation.mapper.RelationMapper;
import com.github.benmanes.caffeine.cache.Cache;
import com.tongji.cache.hotkey.HotKeyDetector;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import java.util.concurrent.ConcurrentHashMap;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.concurrent.ThreadLocalRandom;

@Service
public class KnowPostFeedServiceImpl implements KnowPostFeedService {

    private final KnowPostMapper mapper;
    private final StringRedisTemplate redis;
    private final ObjectMapper objectMapper;
    private final CounterService counterService;
    private final RelationMapper relationMapper;
    private final Cache<String, FeedPageResponse> feedPublicCache;
    private final Cache<String, FeedPageResponse> feedMineCache;
    private final HotKeyDetector hotKey;
    private static final Logger log = LoggerFactory.getLogger(KnowPostFeedServiceImpl.class);
    private static final int LAYOUT_VER = 1;
    private final ConcurrentHashMap<String, Object> singleFlight = new ConcurrentHashMap<>();

    /**
     * 构造函数：注入 Mapper、Redis、对象映射器、计数服务与本地缓存。
     * @param mapper 数据访问层
     * @param redis Redis 客户端
     * @param objectMapper JSON 序列化/反序列化器
     * @param counterService 点赞/收藏计数服务
     * @param feedPublicCache 首页公共 Feed 本地缓存
     * @param feedMineCache 我的发布 Feed 本地缓存
     * @param hotKey 热点 Key 检测器，用于动态延长 TTL
     */
    @Autowired
    public KnowPostFeedServiceImpl(
            KnowPostMapper mapper,
            StringRedisTemplate redis,
            ObjectMapper objectMapper,
            CounterService counterService,
            RelationMapper relationMapper,
            @Qualifier("feedPublicCache") Cache<String, FeedPageResponse> feedPublicCache,
            @Qualifier("feedMineCache") Cache<String, FeedPageResponse> feedMineCache,
            HotKeyDetector hotKey
    ) {
        this.mapper = mapper;
        this.redis = redis;
        this.objectMapper = objectMapper;
        this.counterService = counterService;
        this.relationMapper = relationMapper;
        this.feedPublicCache = feedPublicCache;
        this.feedMineCache = feedMineCache;
        this.hotKey = hotKey;
    }

    /**
     * 生成公共 Feed 页面的缓存 Key（包含分页与布局版本）。
     * @param page 页码（1 起）
     * @param size 每页大小
     * @return Redis/Page 缓存的 Key
     */
    private String cacheKey(int page, int size) {
        return "feed:public:" + size + ":" + page + ":v" + LAYOUT_VER;
    }

    /**
     * 获取公开的首页 Feed（按发布时间倒序，不受置顶影响）。
     * 采用三级缓存：本地 Caffeine、Redis 页面缓存、Redis 片段缓存（ids/item/count）。
     * @param page 页码（≥1）
     * @param size 每页数量（1~50）
     * @param currentUserIdNullable 当前用户 ID（为空表示匿名）
     * @return 带分页信息的 Feed 列表（liked/faved 为用户维度）
     */
    public FeedPageResponse getPublicFeed(int page, int size, Long currentUserIdNullable) {
        int safeSize = Math.min(Math.max(size, 1), 50);
        int safePage = Math.max(page, 1);
        // 这个 localPageKey 是本地缓存的页面 Key（非 Redis）
        // feed:public:20:1:v1
        String localPageKey = cacheKey(safePage, safeSize);

        // 按小时分片的片段缓存键：降低跨小时内容更新导致的大面积失效风险
        // 将分页维度（size/page）与时间维度（hourSlot）组合，避免热门页在整站失效时同时回源
        long hourSlot = System.currentTimeMillis() / 3600000L;
        String idsKey = "feed:public:ids:" + safeSize + ":" + hourSlot + ":" + safePage;
        String hasMoreKey = "feed:public:ids:" + safeSize + ":" + hourSlot + ":" + safePage + ":hasMore";

        // L1: 先从本地缓存拿数据，高并发时抗 80% 流量
        FeedPageResponse local = feedPublicCache.getIfPresent(localPageKey);

        if (local != null && local.items() != null) {
            // 对返回列表中的每个条目进行热度统计
            for (FeedItemResponse item : local.items()) {
                recordItemHotKey(item.id());
            }

            log.info("feed.public source=local localPageKey={} page={} size={}", localPageKey, safePage, safeSize);
            List<FeedItemResponse> enrichedLocal = enrich(local.items(), currentUserIdNullable);

            return new FeedPageResponse(enrichedLocal, local.page(), local.size(), local.hasMore());
        }

        // L2: 二级缓存，Redis 片段缓存，组装
        FeedPageResponse fromCache = assembleFromCache(idsKey, hasMoreKey, safePage, safeSize, currentUserIdNullable);
        if (fromCache != null) {
            feedPublicCache.put(localPageKey, fromCache);
            // 对返回列表中的每个条目进行热度统计
            if (fromCache.items() != null) {
                for (FeedItemResponse item : fromCache.items()) {
                    recordItemHotKey(item.id());
                }
            }
            log.info("feed.public source=redis localPageKey={} page={} size={}", localPageKey, safePage, safeSize);
            return fromCache;
        }

        // 当上述两级缓存都没有数据，说明需要回源查数据库
        // 为了防止高并发下（例如 1000 个请求同时访问同一页）
        // 所有请求同时打到数据库（造成 缓存击穿 ），这里使用了锁
        // 单航班机制：以 idsKey 作为“航班号”
        // 并发下同一页只允许一个请求回源数据库，其余在锁内优先重查缓存，避免击穿惊群
        Object lock = singleFlight.computeIfAbsent(idsKey, k -> new Object());
        synchronized (lock) {
            System.out.println("【单飞锁演示】正在查数据库！page = " + safePage); // <-- 加这一句
            // 重查 L2 缓存，避免重复回源
            FeedPageResponse again = assembleFromCache(idsKey, hasMoreKey, safePage, safeSize, currentUserIdNullable);
            if (again != null) {
                //本地缓存存在15s
                feedPublicCache.put(localPageKey, again);
                // 对返回列表中的每个条目进行热度统计
                if (again.items() != null) {
                    for (FeedItemResponse item : again.items()) {
                        recordItemHotKey(item.id());
                    }
                }
                log.info("feed.public source=3tier(after-flight) localPageKey={} page={} size={}", localPageKey, safePage, safeSize);
                singleFlight.remove(idsKey);
                return again;
            }

            // 数据库回源：读取 size+1 以判断是否有下一页，后裁剪为当前页
            int offset = (safePage - 1) * safeSize;
            List<KnowPostFeedRow> rows = mapper.listFeedPublic(safeSize + 1, offset);
            boolean hasMore = rows.size() > safeSize;
            if (hasMore) {
                rows = rows.subList(0, safeSize);
            }

            // 构建基础列表（计数已填充），liked/faved 置为 null 以免污染用户维度缓存
            List<FeedItemResponse> items = mapRowsToItems(rows, null, false);

            FeedPageResponse respForCache = new FeedPageResponse(items, safePage, safeSize, hasMore);
            // 片段缓存（ids/item/count）TTL 更长并加入随机抖动，降低同一时刻大量过期
            int baseTtl = 60;
            int jitter = ThreadLocalRandom.current().nextInt(30);
            Duration frTtl = Duration.ofSeconds(baseTtl + jitter);//随机TTL为60-90s

            // 写入片段缓存与本地缓存
            //feed:public:20:1:v1  feed:public:ids:" + safeSize + ":" + hourSlot + ":" + safePage;
            writeCaches(localPageKey, idsKey, hasMoreKey, safeSize, rows, items, hasMore, frTtl);
            //本地缓存存在15s
            feedPublicCache.put(localPageKey, respForCache);

            // 返回时覆盖用户维度状态，不写回缓存
            List<FeedItemResponse> enriched = enrich(items, currentUserIdNullable);
            log.info("feed.public source=db localPageKey={} page={} size={} hasMore={}", localPageKey, safePage, safeSize, hasMore);
            // 释放单航班锁，允许后续请求正常进入
            singleFlight.remove(idsKey);

            return new FeedPageResponse(enriched, safePage, safeSize, hasMore);
        }
    }

    /**
     * 记录单个内容条目的热度，并尝试延长其相关片段缓存的 TTL。
     * @param itemId 内容 ID
     */
    @Override
    public FeedPageResponse getRecommendFeed(int page, int size, Long currentUserIdNullable) {
        int safeSize = Math.min(Math.max(size, 1), 50);
        int safePage = Math.max(page, 1);
        String userPart = currentUserIdNullable == null ? "guest" : String.valueOf(currentUserIdNullable);
        String localPageKey = "feed:recommend:" + userPart + ":" + safeSize + ":" + safePage + ":v" + LAYOUT_VER;

        long hourSlot = System.currentTimeMillis() / 3600000L;
        String idsKey = "feed:recommend:ids:" + userPart + ":" + safeSize + ":" + hourSlot + ":" + safePage + ":v" + LAYOUT_VER;
        String hasMoreKey = idsKey + ":hasMore";
        hotKey.record(localPageKey);

        FeedPageResponse local = feedPublicCache.getIfPresent(localPageKey);
        if (local != null && local.items() != null) {
            recordItemHotKeys(local.items());
            log.info("feed.recommend source=local localPageKey={} page={} size={} user={}", localPageKey, safePage, safeSize, userPart);
            return new FeedPageResponse(enrich(local.items(), currentUserIdNullable), safePage, safeSize, local.hasMore());
        }

        FeedPageResponse fromFragments = assembleFromCache(idsKey, hasMoreKey, safePage, safeSize, currentUserIdNullable);
        if (fromFragments != null) {
            FeedPageResponse baseResp = new FeedPageResponse(stripUserFlags(fromFragments.items()), safePage, safeSize, fromFragments.hasMore());
            feedPublicCache.put(localPageKey, baseResp);
            recordItemHotKeys(baseResp.items());
            log.info("feed.recommend source=redis localPageKey={} page={} size={} user={}", localPageKey, safePage, safeSize, userPart);
            return fromFragments;
        }

        Object lock = singleFlight.computeIfAbsent(idsKey, k -> new Object());
        synchronized (lock) {
            FeedPageResponse fragmentsAgain = assembleFromCache(idsKey, hasMoreKey, safePage, safeSize, currentUserIdNullable);
            if (fragmentsAgain != null) {
                FeedPageResponse baseResp = new FeedPageResponse(stripUserFlags(fragmentsAgain.items()), safePage, safeSize, fragmentsAgain.hasMore());
                feedPublicCache.put(localPageKey, baseResp);
                recordItemHotKeys(baseResp.items());
                log.info("feed.recommend source=3tier(after-flight) localPageKey={} page={} size={} user={}", localPageKey, safePage, safeSize, userPart);
                singleFlight.remove(idsKey);
                return fragmentsAgain;
            }

            List<Long> ids = recallRecommendIds(currentUserIdNullable, safePage, safeSize);
            List<KnowPostFeedRow> rows = ids.isEmpty()
                    ? mapper.listFeedPublic(safeSize + 1, (safePage - 1) * safeSize)
                    : loadRowsByIds(ids);
            boolean hasMore = rows.size() > safeSize;
            if (hasMore) {
                rows = rows.subList(0, safeSize);
            }
            List<FeedItemResponse> baseItems = mapRowsToItems(rows, null, false);
            FeedPageResponse baseResp = new FeedPageResponse(baseItems, safePage, safeSize, hasMore);
            try {
                writeRecommendCaches(idsKey, hasMoreKey, safeSize, rows, baseItems, hasMore, recommendTtl(localPageKey));
                feedPublicCache.put(localPageKey, baseResp);
                recordItemHotKeys(baseItems);
            } catch (Exception ignored) {
            } finally {
                singleFlight.remove(idsKey);
            }
            log.info("feed.recommend source=recall localPageKey={} page={} size={} user={} ids={} hasMore={}", localPageKey, safePage, safeSize, userPart, ids.size(), hasMore);
            return new FeedPageResponse(enrich(baseItems, currentUserIdNullable), safePage, safeSize, hasMore);
        }
    }

    @Override
    public FeedPageResponse getFollowingFeed(long userId, int page, int size) {
        int safeSize = Math.min(Math.max(size, 1), 50);
        int safePage = Math.max(page, 1);
        String key = "feed:following:" + userId + ":" + safeSize + ":v" + LAYOUT_VER;
        int start = (safePage - 1) * safeSize;
        int end = start + safeSize;

        Set<String> cachedIds = redis.opsForZSet().reverseRange(key, start, end);
        if (cachedIds == null || cachedIds.size() < Math.min(safeSize + 1, end - start + 1)) {
            Object lock = singleFlight.computeIfAbsent(key, k -> new Object());
            synchronized (lock) {
                try {
                    cachedIds = redis.opsForZSet().reverseRange(key, start, end);
                    if (cachedIds == null || cachedIds.size() < Math.min(safeSize + 1, end - start + 1)) {
                        refillFollowingPool(userId, key);
                    }
                } finally {
                    singleFlight.remove(key);
                }
            }
        }

        cachedIds = redis.opsForZSet().reverseRange(key, start, end);
        List<Long> ids = toLongIds(cachedIds);
        if (ids.isEmpty()) {
            return new FeedPageResponse(List.of(), safePage, safeSize, false);
        }
        boolean hasMore = ids.size() > safeSize;
        if (hasMore) {
            ids = ids.subList(0, safeSize);
        }
        List<KnowPostFeedRow> rows = loadRowsByIds(ids);
        List<FeedItemResponse> items = mapRowsToItems(rows, userId, false);
        log.info("feed.following source=zset key={} page={} size={} user={} hasMore={}", key, safePage, safeSize, userId, hasMore);
        return new FeedPageResponse(items, safePage, safeSize, hasMore);
    }

    private List<Long> recallRecommendIds(Long userId, int page, int size) {
        int start = (page - 1) * size;
        int need = start + size + 1;
        LinkedHashSet<Long> ids = new LinkedHashSet<>();
        if (userId != null) {
            Set<String> tags = redis.opsForZSet().reverseRange("feed:user:interest:" + userId, 0, 4);
            if (tags != null) {
                for (String tag : tags) {
                    ids.addAll(toLongIds(redis.opsForZSet().reverseRange("feed:recall:tag:" + tag, 0, 120)));
                    if (ids.size() >= need) {
                        break;
                    }
                }
            }
        }
        if (ids.size() < need) {
            ids.addAll(toLongIds(redis.opsForZSet().reverseRange("feed:recall:hot", 0, 200)));
        }
        if (ids.size() <= start) {
            return List.of();
        }
        List<Long> all = new ArrayList<>(ids);
        return all.subList(start, Math.min(all.size(), need));
    }

    private void refillFollowingPool(long userId, String key) {
        List<Long> following = relationMapper.listFollowing(userId, 500, 0);
        if (following == null || following.isEmpty()) {
            redis.delete(key);
            return;
        }
        List<KnowPostFeedRow> rows = mapper.listFeedByCreatorIds(following, 500, 0);
        redis.delete(key);
        for (KnowPostFeedRow row : rows) {
            long score = row.getPublishTime() == null ? System.currentTimeMillis() : row.getPublishTime().toEpochMilli();
            redis.opsForZSet().add(key, String.valueOf(row.getId()), score);
        }
        redis.expire(key, Duration.ofSeconds(60 + ThreadLocalRandom.current().nextInt(30)));
    }

    private List<KnowPostFeedRow> loadRowsByIds(List<Long> ids) {
        if (ids == null || ids.isEmpty()) {
            return List.of();
        }
        List<KnowPostFeedRow> rows = mapper.listFeedByIds(ids);
        Map<Long, KnowPostFeedRow> byId = new LinkedHashMap<>();
        for (KnowPostFeedRow row : rows) {
            byId.put(row.getId(), row);
        }
        List<KnowPostFeedRow> ordered = new ArrayList<>();
        for (Long id : ids) {
            KnowPostFeedRow row = byId.get(id);
            if (row != null) {
                ordered.add(row);
            }
        }
        return ordered;
    }

    private List<Long> toLongIds(Set<String> ids) {
        if (ids == null || ids.isEmpty()) {
            return List.of();
        }
        List<Long> result = new ArrayList<>(ids.size());
        for (String id : ids) {
            try {
                result.add(Long.valueOf(id));
            } catch (NumberFormatException ignored) {
            }
        }
        return result;
    }

    private void recordItemHotKey(String itemId) {
        // 使用内容 ID 作为热点统计 Key，而不是页面 Key
        String hotKeyId = "knowpost:" + itemId;
        hotKey.record(hotKeyId);
        
        int baseTtl = 60;
        int target = hotKey.ttlForPublic(baseTtl, hotKeyId);
        
        // 延长该内容的详情片段缓存
        String itemKey = "feed:item:" + itemId;
        Long itemTtl = redis.getExpire(itemKey);
        if (itemTtl < target) {
            redis.expire(itemKey, Duration.ofSeconds(target));
        }
    }

    private void recordItemHotKeys(List<FeedItemResponse> items) {
        if (items == null) {
            return;
        }
        for (FeedItemResponse item : items) {
            if (item != null && item.id() != null) {
                recordItemHotKey(item.id());
            }
        }
    }

    private Duration recommendTtl(String key) {
        int baseTtl = 45;
        int jitter = ThreadLocalRandom.current().nextInt(20);
        return Duration.ofSeconds(hotKey.ttlForPublic(baseTtl + jitter, key));
    }

    private void maybeExtendTtlRecommend(String pageKey, String idsKey, String hasMoreKey) {
        int target = hotKey.ttlForPublic(45, pageKey);
        extendIfShorter(idsKey, target);
        extendIfShorter(hasMoreKey, Math.min(target, 30));
    }

    private void extendIfShorter(String key, int targetSeconds) {
        Long currentTtl = redis.getExpire(key);
        if (currentTtl != null && currentTtl >= 0 && currentTtl < targetSeconds) {
            redis.expire(key, Duration.ofSeconds(targetSeconds));
        }
    }

    private void writeRecommendCaches(String idsKey,
                                      String hasMoreKey,
                                      int size,
                                      List<KnowPostFeedRow> rows,
                                      List<FeedItemResponse> items,
                                      boolean hasMore,
                                      Duration ttl) {
        redis.delete(idsKey);
        List<String> idVals = new ArrayList<>();
        for (KnowPostFeedRow row : rows) {
            idVals.add(String.valueOf(row.getId()));
        }
        if (!idVals.isEmpty()) {
            redis.opsForList().rightPushAll(idsKey, idVals);
            redis.expire(idsKey, ttl);
            Duration hasMoreTtl = idVals.size() == size && hasMore
                    ? Duration.ofSeconds(10 + ThreadLocalRandom.current().nextInt(11))
                    : Duration.ofSeconds(10);
            redis.opsForValue().set(hasMoreKey, hasMore ? "1" : "0", hasMoreTtl);
        }

        for (FeedItemResponse item : items) {
            try {
                String itemKey = "feed:item:" + item.id();
                redis.opsForValue().set(itemKey, objectMapper.writeValueAsString(item), ttl);
            } catch (Exception ignored) {
            }
        }
    }

    private List<FeedItemResponse> stripUserFlags(List<FeedItemResponse> items) {
        if (items == null || items.isEmpty()) {
            return List.of();
        }
        List<FeedItemResponse> result = new ArrayList<>(items.size());
        for (FeedItemResponse item : items) {
            result.add(new FeedItemResponse(
                    item.id(),
                    item.title(),
                    item.description(),
                    item.coverImage(),
                    item.tags(),
                    item.authorAvatar(),
                    item.authorNickname(),
                    item.tagJson(),
                    item.likeCount(),
                    item.favoriteCount(),
                    null,
                    null,
                    item.isTop()
            ));
        }
        return result;
    }

    /**
     * 叠加用户维度状态，将 liked/faved 根据用户计算覆盖到列表上。
     * 不改写底层缓存，避免不同用户状态互相污染。
     * @param base 基础列表（含计数）
     * @param uid 用户 ID（可空）
     * @return 叠加 liked/faved 的列表
     */
    private List<FeedItemResponse> enrich(List<FeedItemResponse> base, Long uid) {
        List<FeedItemResponse> out = new ArrayList<>(base.size());

        for (FeedItemResponse it : base) {

            Map<String, Long> counts = counterService.getCounts("knowpost", String.valueOf(it.id()), List.of("like", "fav"));
            Long likeCount = counts.getOrDefault("like", 0L);
            Long favoriteCount = counts.getOrDefault("fav", 0L);

            boolean liked = uid != null && counterService.isLiked("knowpost", it.id(), uid);
            boolean faved = uid != null && counterService.isFaved("knowpost", it.id(), uid);
            out.add(new FeedItemResponse(
                    it.id(),
                    it.title(),
                    it.description(),
                    it.coverImage(),
                    it.tags(),
                    it.authorAvatar(),
                    it.authorNickname(),
                    it.tagJson(),
                    likeCount,
                    favoriteCount,
                    liked,
                    faved,
                    it.isTop()
            ));
        }
        return out;
    }

    /**
     * 从 Redis 片段缓存组装页面：
     * - idsKey：列表 ID 顺序
     * - itemKey：每个条目基础信息
     * - countKey：点赞/收藏计数
     * 若缺片段则回源修补并写回软缓存。
     * @param idsKey Redis 列表 Key
     * @param hasMoreKey Redis 软缓存 hasMore Key
     * @param page 页码
     * @param size 每页大小
     * @param uid 当前用户 ID（用于 liked/faved）
     * @return 组装完成的页面；不存在时返回 null
     */
    private FeedPageResponse assembleFromCache(String idsKey, String hasMoreKey, int page, int size, Long uid) {
        // 需要展示知文的 ID 列表
        List<String> idList = redis.opsForList().range(idsKey, 0, size - 1);
        String hasMoreStr = redis.opsForValue().get(hasMoreKey);
        if (idList == null || idList.isEmpty()) {
            return null;
        }

        // 构造内容元数据（标题，内容等）的 Redis Key
        List<String> itemKeys = new ArrayList<>(idList.size());
        for (String id : idList) {
            itemKeys.add("feed:item:" + id);
        }
        // 批量获取知文 元数据
        List<String> itemJsons = redis.opsForValue().multiGet(itemKeys);

        List<FeedItemResponse> items = new ArrayList<>(idList.size());

        for (int i = 0; i < idList.size(); i++) {
            String itemJson = (itemJsons != null && i < itemJsons.size()) ? itemJsons.get(i) : null;
            if (itemJson == null) {
                // 缺失元数据片段，触发回源
                return null;
            }

            try {
                items.add(objectMapper.readValue(itemJson, FeedItemResponse.class));
            } catch (Exception e) {
                return null;
            }
        }

        List<FeedItemResponse> enriched = new ArrayList<>(idList.size());
        for (int i = 0; i < idList.size(); i++) {
            FeedItemResponse base = items.get(i);
            if (base == null) {
                continue;
            }

            Map<String, Long> counts = counterService.getCounts("knowpost", String.valueOf(base.id()), List.of("like", "fav"));
            Long likeCount = counts.getOrDefault("like", 0L);
            Long favoriteCount = counts.getOrDefault("fav", 0L);

            // 用户维度状态实时计算，不落入片段缓存以避免用户数据污染
            boolean liked = uid != null && counterService.isLiked("knowpost", base.id(), uid);
            boolean faved = uid != null && counterService.isFaved("knowpost", base.id(), uid);

            enriched.add(new FeedItemResponse(
                    base.id(),
                    base.title(),
                    base.description(),
                    base.coverImage(),
                    base.tags(),
                    base.authorAvatar(),
                    base.authorNickname(),
                    base.tagJson(),
                    likeCount,
                    favoriteCount,
                    liked,
                    faved,
                    base.isTop())
            );
        }
        // hasMore 优先使用软缓存值；若缺失，则以“满页”作为兜底判断
        boolean hasMore = hasMoreStr != null ? "1".equals(hasMoreStr) : (idList.size() == size);

        return new FeedPageResponse(enriched, page, size, hasMore);
    }

    /**
     * 写入片段缓存与软缓存：
     * - idsKey：ID 列表（中 TTL）
     * - item：条目片段（中 TTL）
     * - hasMore：软缓存，满页时缓存 true 10~20s，否则 10s
     * 注意：不再写入 Redis 整页缓存 (pageKey)，避免双重存储。
     * @param pageKey 页面缓存 Key (用于反向索引引用)
     * @param idsKey ID 列表 Key
     * @param hasMoreKey 软缓存 Key
     * @param size 每页大小
     * @param rows 原始行数据
     * @param items 条目列表（计数已填充，liked/faved 为空）
     * @param hasMore 是否还有更多
     * @param frTtl 片段缓存 TTL
     */
    private void writeCaches(String pageKey, String idsKey, String hasMoreKey, int size, List<KnowPostFeedRow> rows, List<FeedItemResponse> items, boolean hasMore, Duration frTtl) {
        List<String> idVals = new ArrayList<>();

        for (KnowPostFeedRow r : rows) {
            idVals.add(String.valueOf(r.getId()));
        }

        if (!idVals.isEmpty()) {
            //写入Redis：feed:public:ids:20:492271:1 知文列表 60-90s
            redis.opsForList().leftPushAll(idsKey, idVals);
            redis.expire(idsKey, frTtl);
            // 软缓存 hasMore：仅在满页时缓存 true，TTL 很短
            if (idVals.size() == size && hasMore) {
                redis.opsForValue().set(hasMoreKey, "1", Duration.ofSeconds(10 + ThreadLocalRandom.current().nextInt(11)));
            } else {
                redis.opsForValue().set(hasMoreKey, hasMore ? "1" : "0", Duration.ofSeconds(10));
            }
        }

        // 页面键集合索引，用于按页面维度批量失效与清理（即使没有 Redis 整页缓存，依然保留反向索引用于本地缓存通知或其他用途）
        // 写入Redis feed:public:pages feed:public:20:1:v1 永久
        redis.opsForSet().add("feed:public:pages", pageKey);

        for (FeedItemResponse it : items) {
            // 反向索引：按小时为每个内容建立“页面引用关系”，支持内容更新时快速定位受影响页面
            long hourSlot = System.currentTimeMillis() / 3600000L;
            //写入Redis feed:public:index:273055120273575936:492271 feed:public:20:1:v1 60-90s
            String idxKey = "feed:public:index:" + it.id() + ":" + hourSlot;
            redis.opsForSet().add(idxKey, pageKey);
            redis.expire(idxKey, frTtl);

            try {
                String itemKey = "feed:item:" + it.id();
                String itemJson = objectMapper.writeValueAsString(it);
                //写入Redis片段缓存 feed:item:273055120273575936 内容：每个知文 60-90s
                redis.opsForValue().set(itemKey, itemJson, frTtl);
            } catch (Exception ignored) {}
        }
    }

    /**
     * 生成“我的发布”列表的缓存 Key（用户维度）。
     * @param userId 用户 ID
     * @param page 页码
     * @param size 每页大小
     * @return Redis 页面缓存 Key
     */
    private String myCacheKey(long userId, int page, int size) {
        return "feed:mine:" + userId + ":" + size + ":" + page;
    }

    /**
     * 获取当前用户自己发布的知文列表（按发布时间倒序）。
     * 缓存策略：本地 Caffeine + Redis 页面缓存（TTL 更短）。
     * 返回的每条目包含 isTop 字段以表示是否置顶。
     * @param userId 当前用户 ID
     * @param page 页码（≥1）
     * @param size 每页数量（1~50）
     * @return 带分页信息的个人发布列表
     */
    public FeedPageResponse getMyPublished(long userId, int page, int size) {
        int safeSize = Math.min(Math.max(size, 1), 50);
        int safePage = Math.max(page, 1);
        String key = myCacheKey(userId, safePage, safeSize);

        FeedPageResponse local = feedMineCache.getIfPresent(key);
        if (local != null) {
            hotKey.record(key);
            maybeExtendTtlMine(key);
            log.info("feed.mine source=local key={} page={} size={} user={}", key, safePage, safeSize, userId);
            return local;
        }

        String cached = redis.opsForValue().get(key);
        if (cached != null) {
            try {
                FeedPageResponse cachedResp = objectMapper.readValue(cached, FeedPageResponse.class);
                boolean hasCounts = cachedResp.items() != null && cachedResp.items().stream()
                        .allMatch(it -> it.likeCount() != null && it.favoriteCount() != null);
                if (hasCounts) {
                    // 覆盖 liked/faved，确保老缓存也能返回用户维度状态
                    feedMineCache.put(key, cachedResp);
                    hotKey.record(key);
                    maybeExtendTtlMine(key);
                    log.info("feed.mine source=page key={} page={} size={} user={}", key, safePage, safeSize, userId);
                List<FeedItemResponse> enriched = enrich(cachedResp.items(), userId);
                return new FeedPageResponse(enriched, cachedResp.page(), cachedResp.size(), cachedResp.hasMore());
            }
            } catch (Exception ignored) {}
        }

        int offset = (safePage - 1) * safeSize;
        List<KnowPostFeedRow> rows = mapper.listMyPublished(userId, safeSize + 1, offset);
        boolean hasMore = rows.size() > safeSize;
        if (hasMore) rows = rows.subList(0, safeSize);

        List<FeedItemResponse> items = mapRowsToItems(rows, userId, true);

        FeedPageResponse resp = new FeedPageResponse(items, safePage, safeSize, hasMore);
        try {
            String json = objectMapper.writeValueAsString(resp);
            int baseTtl = 30; // 用户维度列表缓存更短
            int jitter = ThreadLocalRandom.current().nextInt(20);
            redis.opsForValue().set(key, json, Duration.ofSeconds(baseTtl + jitter));
            feedMineCache.put(key, resp);
            hotKey.record(key);
        } catch (Exception ignored) {}
        log.info("feed.mine source=db key={} page={} size={} user={} hasMore={}", key, safePage, safeSize, userId, hasMore);
        return resp;
    }

    /**
     * 解析 JSON 数组字符串为 List<String>。
     * @param json JSON 数组字符串
     * @return 字符串列表；解析失败或空字符串返回空列表
     */
    private List<String> parseStringArray(String json) {
        if (json == null || json.isBlank()) return Collections.emptyList();
        try {
            return objectMapper.readValue(json, new TypeReference<>() {
            });
        } catch (Exception e) {
            return Collections.emptyList();
        }
    }

    /**
     * 将数据库行映射为响应条目。
     * 计数通过计数服务填充；liked/faved 按需计算；isTop 仅在个人列表返回。
     * @param rows 查询结果行
     * @param userIdNullable 当前用户 ID（可空）
     * @param includeIsTop 是否在响应中包含 isTop
     * @return 条目列表
     */
    private List<FeedItemResponse> mapRowsToItems(List<KnowPostFeedRow> rows, Long userIdNullable, boolean includeIsTop) {
        List<FeedItemResponse> items = new ArrayList<>(rows.size());

        for (KnowPostFeedRow r : rows) {
            List<String> tags = parseStringArray(r.getTags());
            List<String> imgs = parseStringArray(r.getImgUrls());
            String cover = imgs.isEmpty() ? null : imgs.getFirst();

            Map<String, Long> counts = counterService.getCounts("knowpost", String.valueOf(r.getId()), List.of("like", "fav"));
            Long likeCount = counts.getOrDefault("like", 0L);
            Long favoriteCount = counts.getOrDefault("fav", 0L);

            Boolean liked = userIdNullable != null && counterService.isLiked("knowpost", String.valueOf(r.getId()), userIdNullable);
            Boolean faved = userIdNullable != null && counterService.isFaved("knowpost", String.valueOf(r.getId()), userIdNullable);
            Boolean isTop = includeIsTop ? r.getIsTop() : null;

            items.add(new FeedItemResponse(
                    String.valueOf(r.getId()),
                    r.getTitle(),
                    r.getDescription(),
                    cover,
                    tags,
                    r.getAuthorAvatar(),
                    r.getAuthorNickname(),
                    r.getAuthorTagJson(),
                    likeCount,
                    favoriteCount,
                    liked,
                    faved,
                    isTop
            ));
        }
        return items;
    }



    /**
     * 根据热点级别动态延长“我的发布”页面缓存 TTL。
     * @param key 页面缓存 Key
     */
    private void maybeExtendTtlMine (String key) {
        int baseTtl = 30;
        int target = hotKey.ttlForMine(baseTtl, key);
        Long currentTtl = redis.getExpire(key);
        if (currentTtl < target) {
            redis.expire(key, Duration.ofSeconds(target));
        }
    }
}
