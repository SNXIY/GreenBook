package com.tongji.auth.token;

import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Objects;
import java.util.Set;

@Component
public class RedisRefreshTokenStore implements RefreshTokenStore {
    private static final String SESSION_PREFIX = "auth:session:";
    private static final String USER_SESSIONS_PREFIX = "auth:user:sessions:";

    private final StringRedisTemplate redisTemplate;

    public RedisRefreshTokenStore(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    @Override
    public void storeToken(long userId, String sessionId, String refreshTokenId, Duration ttl) {
        String sessionKey = sessionKey(userId, sessionId);
        redisTemplate.opsForValue().set(sessionKey, refreshTokenId, ttl);
        String sessionsKey = userSessionsKey(userId);
        redisTemplate.opsForSet().add(sessionsKey, sessionId);
        redisTemplate.expire(sessionsKey, ttl);
    }

    @Override
    public boolean isTokenValid(long userId, String sessionId, String refreshTokenId) {
        String currentRefreshTokenId = redisTemplate.opsForValue().get(sessionKey(userId, sessionId));
        return Objects.equals(currentRefreshTokenId, refreshTokenId);
    }

    @Override
    public void revokeToken(long userId, String sessionId) {
        redisTemplate.delete(sessionKey(userId, sessionId));
        redisTemplate.opsForSet().remove(userSessionsKey(userId), sessionId);
    }

    @Override
    public void revokeAll(long userId) {
        String sessionsKey = userSessionsKey(userId);
        Set<String> sessionIds = redisTemplate.opsForSet().members(sessionsKey);
        if (sessionIds != null && !sessionIds.isEmpty()) {
            for (String sessionId : sessionIds) {
                redisTemplate.delete(sessionKey(userId, sessionId));
            }
        }
        redisTemplate.delete(sessionsKey);
    }

    private static String sessionKey(long userId, String sessionId) {
        return SESSION_PREFIX + userId + ":" + sessionId;
    }

    private static String userSessionsKey(long userId) {
        return USER_SESSIONS_PREFIX + userId;
    }
}
