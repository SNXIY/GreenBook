package com.tongji.assistant.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.assistant.api.dto.AssistantCapabilityRequest;
import com.tongji.assistant.api.dto.AssistantCapabilityResponse;
import com.tongji.assistant.mapper.AssistantCapabilityMapper;
import com.tongji.auth.config.AuthProperties;
import com.tongji.auth.token.JwtService;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import lombok.RequiredArgsConstructor;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.security.oauth2.jwt.JwtException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class AssistantCapabilityService {
    private static final String CAPABILITY_AUDIENCE = "zhiguang-assistant-tools";
    private static final Set<String> ALLOWED_ACTIONS = Set.of(
            "community.search_posts",
            "community.get_post",
            "community.get_own_draft",
            "community.analyze_engagement",
            "community.list_own_posts",
            "community.delete_post",
            "community.delete_own_posts_batch",
            "publication.publish_now",
            "community.reply_comment"
    );

    private final JwtEncoder jwtEncoder;
    private final JwtService jwtService;
    private final AuthProperties authProperties;
    private final AssistantCapabilityMapper mapper;
    private final ObjectMapper objectMapper;

    @Transactional
    public AssistantCapabilityResponse issue(
            String delegatedUserToken,
            AssistantCapabilityRequest request
    ) {
        Jwt userJwt = decodeUserAccessToken(delegatedUserToken);
        long userId = jwtService.extractUserId(userJwt);
        List<String> actions = request.actions().stream()
                .map(String::trim)
                .collect(Collectors.collectingAndThen(
                        Collectors.toCollection(LinkedHashSet::new),
                        List::copyOf
                ));
        if (actions.size() != 1 || !ALLOWED_ACTIONS.containsAll(actions)) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "请求包含未授权的助手能力");
        }
        List<String> resources = request.resources() == null
                ? List.of()
                : request.resources().stream()
                .map(String::trim)
                .collect(Collectors.collectingAndThen(
                        Collectors.toCollection(LinkedHashSet::new),
                        List::copyOf
                ));
        validateResources(actions.getFirst(), resources);
        Instant issuedAt = Instant.now();
        Instant expiresAt = issuedAt.plusSeconds(request.ttlSeconds());
        String capabilityId = UUID.randomUUID().toString();
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(authProperties.getJwt().getIssuer())
                .audience(List.of(CAPABILITY_AUDIENCE))
                .issuedAt(issuedAt)
                .expiresAt(expiresAt)
                .subject(String.valueOf(userId))
                .id(capabilityId)
                .claim("token_type", "capability")
                .claim("actor_uid", userId)
                .claim("run_id", request.runId())
                .claim("actions", actions)
                .claim("resources", resources)
                .claim("max_uses", request.maxUses())
                .build();
        mapper.insert(
                capabilityId,
                request.runId(),
                userId,
                toJson(actions),
                toJson(resources),
                request.maxUses(),
                expiresAt
        );
        String token = jwtEncoder.encode(JwtEncoderParameters.from(claims)).getTokenValue();
        return new AssistantCapabilityResponse(token, capabilityId, expiresAt);
    }

    @Transactional
    public CapabilityPrincipal authorize(
            String capabilityToken,
            String action,
            List<String> requiredResources
    ) {
        Jwt jwt;
        try {
            jwt = jwtService.decode(capabilityToken);
        } catch (JwtException ex) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "助手能力令牌无效或已过期");
        }
        if (!"capability".equals(jwt.getClaimAsString("token_type"))
                || jwt.getAudience() == null
                || !jwt.getAudience().contains(CAPABILITY_AUDIENCE)) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "令牌不是助手能力令牌");
        }
        List<String> actions = jwt.getClaimAsStringList("actions");
        List<String> resources = jwt.getClaimAsStringList("resources");
        if (actions == null || !actions.contains(action)) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "能力令牌不允许该操作");
        }
        if (requiredResources != null && !requiredResources.isEmpty()
                && (resources == null || !resources.containsAll(requiredResources))) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "能力令牌不允许访问该资源");
        }
        if (jwt.getId() == null || mapper.consume(jwt.getId()) != 1) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "能力令牌已撤销或使用次数已耗尽");
        }
        Object actor = jwt.getClaims().get("actor_uid");
        if (!(actor instanceof Number number)) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "能力令牌缺少用户身份");
        }
        String runId = jwt.getClaimAsString("run_id");
        if (runId == null || runId.isBlank()) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "能力令牌缺少任务身份");
        }
        return new CapabilityPrincipal(number.longValue(), runId, jwt.getId());
    }

    @Transactional
    public void revoke(String delegatedUserToken, String capabilityId) {
        Jwt userJwt = decodeUserAccessToken(delegatedUserToken);
        long userId = jwtService.extractUserId(userJwt);
        try {
            UUID.fromString(capabilityId);
        } catch (IllegalArgumentException ex) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "能力令牌 ID 格式不正确");
        }
        // Idempotent and non-enumerating: a missing, expired, already revoked, or
        // foreign capability produces the same successful outcome.
        mapper.revoke(capabilityId, userId);
    }

    private Jwt decodeUserAccessToken(String token) {
        if (token == null || token.isBlank()) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "缺少用户授权令牌");
        }
        try {
            Jwt jwt = jwtService.decode(token);
            if (!"access".equals(jwtService.extractTokenType(jwt))
                    || jwt.getAudience() == null
                    || !jwt.getAudience().contains("community-assistant-agent")) {
                throw new BusinessException(ErrorCode.UNAUTHORIZED, "只能使用访问令牌授权助手");
            }
            String role = jwt.getClaimAsString("role");
            if (!"USER".equals(role)) {
                throw new BusinessException(ErrorCode.FORBIDDEN, "只有社区用户可以委托助手");
            }
            return jwt;
        } catch (JwtException ex) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "用户授权令牌无效或已过期");
        }
    }

    private void validateResources(String action, List<String> resources) {
        boolean valid = switch (action) {
            case "community.search_posts", "community.analyze_engagement",
                    "community.list_own_posts" ->
                    resources.isEmpty();
            case "community.get_post", "community.get_own_draft",
                    "community.delete_post", "publication.publish_now" ->
                    resources.size() == 1 && isPositiveIdResource(resources.getFirst(), "post:");
            case "community.delete_own_posts_batch" ->
                    !resources.isEmpty()
                            && resources.size() <= 20
                            && resources.stream().allMatch(
                                    value -> isPositiveIdResource(value, "post:")
                            );
            case "community.reply_comment" ->
                    resources.size() == 2
                            && resources.stream().anyMatch(value -> isPositiveIdResource(value, "post:"))
                            && resources.stream().anyMatch(value -> isPositiveIdResource(value, "comment:"));
            default -> false;
        };
        if (!valid) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "能力资源范围不符合动作契约");
        }
    }

    private boolean isPositiveIdResource(String resource, String prefix) {
        if (!resource.startsWith(prefix)) {
            return false;
        }
        try {
            return Long.parseLong(resource.substring(prefix.length())) > 0;
        } catch (NumberFormatException ex) {
            return false;
        }
    }

    private String toJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (JsonProcessingException ex) {
            throw new BusinessException(ErrorCode.INTERNAL_ERROR, "能力令牌序列化失败");
        }
    }

    public record CapabilityPrincipal(long userId, String runId, String capabilityId) {
    }
}
