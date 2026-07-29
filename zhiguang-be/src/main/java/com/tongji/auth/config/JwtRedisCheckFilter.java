package com.tongji.auth.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.RequiredArgsConstructor;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.Map;

@RequiredArgsConstructor
public class JwtRedisCheckFilter extends OncePerRequestFilter {
    private static final String SESSION_PREFIX = "auth:session:";

    private final StringRedisTemplate redisTemplate;
    private final ObjectMapper objectMapper;

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain filterChain) throws ServletException, IOException {
        Authentication auth = SecurityContextHolder.getContext().getAuthentication();
        if (!(auth instanceof JwtAuthenticationToken jwtToken)) {
            filterChain.doFilter(request, response);
            return;
        }

        Jwt jwt = jwtToken.getToken();
        String userId = jwt.getSubject();
        String tokenType = claimAsString(jwt, "token_type");
        String sessionId = claimAsString(jwt, "sid");

        if (userId == null || sessionId == null || !"access".equals(tokenType)) {
            unauthorized(response, "无效令牌");
            return;
        }

        Boolean exists = redisTemplate.hasKey(SESSION_PREFIX + userId + ":" + sessionId);
        if (!Boolean.TRUE.equals(exists)) {
            unauthorized(response, "您已下线，请重新登录");
            return;
        }

        filterChain.doFilter(request, response);
    }

    private String claimAsString(Jwt jwt, String name) {
        Object claim = jwt.getClaims().get(name);
        return claim == null ? null : claim.toString();
    }

    private void unauthorized(HttpServletResponse response, String message) throws IOException {
        response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
        response.setContentType("application/json;charset=UTF-8");
        response.getWriter().write(objectMapper.writeValueAsString(
                Map.of("code", 401, "msg", message)
        ));
    }
}
