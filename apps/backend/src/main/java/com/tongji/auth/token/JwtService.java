package com.tongji.auth.token;

import com.tongji.auth.config.AuthProperties;
import com.tongji.user.domain.User;
import com.tongji.user.domain.UserRole;
import lombok.RequiredArgsConstructor;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

@Service
@RequiredArgsConstructor
public class JwtService {
    private static final String CLAIM_TOKEN_TYPE = "token_type";
    private static final String CLAIM_USER_ID = "uid";
    private static final String CLAIM_SESSION_ID = "sid";
    private static final String CLAIM_ROLE = "role";

    private final JwtEncoder jwtEncoder;
    private final JwtDecoder jwtDecoder;
    private final AuthProperties properties;

    public TokenPair issueTokenPair(User user) {
        return issueTokenPair(user, UUID.randomUUID().toString());
    }

    public TokenPair rotateTokenPair(User user, String sessionId) {
        return issueTokenPair(user, sessionId);
    }

    public Jwt decode(String token) {
        return jwtDecoder.decode(token);
    }

    private TokenPair issueTokenPair(User user, String sessionId) {
        String refreshTokenId = UUID.randomUUID().toString();
        Instant issuedAt = Instant.now();
        Instant accessExpiresAt = issuedAt.plus(properties.getJwt().getAccessTokenTtl());
        Instant refreshExpiresAt = issuedAt.plus(properties.getJwt().getRefreshTokenTtl());

        String accessToken = encodeToken(
                user, issuedAt, accessExpiresAt, "access", UUID.randomUUID().toString(), sessionId);
        String refreshToken = encodeToken(
                user, issuedAt, refreshExpiresAt, "refresh", refreshTokenId, sessionId);

        return new TokenPair(accessToken, accessExpiresAt, refreshToken, refreshExpiresAt, sessionId, refreshTokenId);
    }

    private String encodeToken(User user,
                               Instant issuedAt,
                               Instant expiresAt,
                               String tokenType,
                               String tokenId,
                               String sessionId) {
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(properties.getJwt().getIssuer())
                .audience(List.of("zhiguang-api", "greenbook-agent-runtime"))
                .issuedAt(issuedAt)
                .expiresAt(expiresAt)
                .subject(String.valueOf(user.getId()))
                .id(tokenId)
                .claim(CLAIM_TOKEN_TYPE, tokenType)
                .claim(CLAIM_USER_ID, user.getId())
                .claim(CLAIM_SESSION_ID, sessionId)
                .claim(CLAIM_ROLE, roleOf(user))
                .claim("roles", List.of("CREATOR", roleOf(user)))
                .claim("tenant_id", "zhiguang")
                .claim("creator_id", String.valueOf(user.getId()))
                .claim("nickname", user.getNickname())
                .build();
        return jwtEncoder.encode(JwtEncoderParameters.from(claims)).getTokenValue();
    }

    private String roleOf(User user) {
        return user.getRole() == null ? UserRole.USER.name() : user.getRole().name();
    }

    public long extractUserId(Jwt jwt) {
        Object claim = jwt.getClaims().get(CLAIM_USER_ID);
        if (claim instanceof Number number) {
            return number.longValue();
        }
        if (claim instanceof String text) {
            return Long.parseLong(text);
        }
        throw new IllegalArgumentException("Invalid user id in token");
    }

    public String extractTokenType(Jwt jwt) {
        Object claim = jwt.getClaims().get(CLAIM_TOKEN_TYPE);
        return claim != null ? claim.toString() : "";
    }

    public String extractTokenId(Jwt jwt) {
        return jwt.getId();
    }

    public String extractSessionId(Jwt jwt) {
        Object claim = jwt.getClaims().get(CLAIM_SESSION_ID);
        return claim != null ? claim.toString() : "";
    }
}
