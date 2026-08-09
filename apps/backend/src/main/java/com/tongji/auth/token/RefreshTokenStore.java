package com.tongji.auth.token;

import java.time.Duration;

public interface RefreshTokenStore {
    void storeToken(long userId, String sessionId, String refreshTokenId, Duration ttl);

    boolean isTokenValid(long userId, String sessionId, String refreshTokenId);

    void revokeToken(long userId, String sessionId);

    void revokeAll(long userId);
}
