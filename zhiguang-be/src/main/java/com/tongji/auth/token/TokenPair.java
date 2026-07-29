package com.tongji.auth.token;

import java.time.Instant;

public record TokenPair(
        String accessToken,
        Instant accessTokenExpiresAt,
        String refreshToken,
        Instant refreshTokenExpiresAt,
        String sessionId,
        String refreshTokenId
) {
}
