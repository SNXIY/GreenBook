package com.tongji.knowpost.service.impl;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.counter.service.CounterService;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.service.FeedIndexService;
import lombok.RequiredArgsConstructor;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.Collections;
import java.util.List;

@Service
@RequiredArgsConstructor
public class FeedIndexServiceImpl implements FeedIndexService {
    public static final String HOT_POOL = "feed:recall:hot";
    public static final String TAG_POOL_PREFIX = "feed:recall:tag:";
    public static final String USER_INTEREST_PREFIX = "feed:user:interest:";
    public static final String FOLLOWING_POOL_PREFIX = "feed:following:";

    private final KnowPostMapper knowPostMapper;
    private final CounterService counterService;
    private final StringRedisTemplate redis;
    private final ObjectMapper objectMapper;

    @Override
    public void indexPublishedPost(long postId) {
        KnowPost post = knowPostMapper.findById(postId);
        if (post == null || !"published".equals(post.getStatus()) || !"public".equals(post.getVisible())) {
            return;
        }
        long publishScore = post.getPublishTime() == null ? System.currentTimeMillis() : post.getPublishTime().toEpochMilli();
        redis.opsForZSet().add(HOT_POOL, String.valueOf(postId), publishScore);
        redis.expire(HOT_POOL, Duration.ofDays(7));

        for (String tag : parseTags(post.getTags())) {
            String key = TAG_POOL_PREFIX + tag;
            redis.opsForZSet().add(key, String.valueOf(postId), publishScore);
            redis.expire(key, Duration.ofDays(7));
        }
        redis.delete("feed:recommend:guest:pages");
    }

    @Override
    public void recordInteraction(long userId, long postId, String metric, int delta) {
        KnowPost post = knowPostMapper.findById(postId);
        if (post == null || !"published".equals(post.getStatus()) || !"public".equals(post.getVisible())) {
            return;
        }
        double hotDelta = switch (metric) {
            case "like" -> 30D;
            case "fav" -> 50D;
            case "view" -> 5D;
            default -> 1D;
        };
        redis.opsForZSet().incrementScore(HOT_POOL, String.valueOf(postId), hotDelta * delta);
        redis.expire(HOT_POOL, Duration.ofDays(7));

        double interestDelta = Math.max(1D, hotDelta / 10D) * Math.max(delta, 1);
        for (String tag : parseTags(post.getTags())) {
            redis.opsForZSet().incrementScore(USER_INTEREST_PREFIX + userId, tag, interestDelta);
            redis.expire(USER_INTEREST_PREFIX + userId, Duration.ofDays(30));
        }
    }

    @Override
    public void invalidateFeedCaches() {
        redis.delete("feed:recommend:guest:pages");
    }

    private List<String> parseTags(String tagsJson) {
        if (tagsJson == null || tagsJson.isBlank()) {
            return Collections.emptyList();
        }
        try {
            return objectMapper.readValue(tagsJson, new TypeReference<>() {});
        } catch (Exception ex) {
            return Collections.emptyList();
        }
    }
}
