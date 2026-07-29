package com.tongji.auth.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.convert.converter.Converter;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.security.authentication.AbstractAuthenticationToken;
import org.springframework.security.core.GrantedAuthority;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationConverter;
import org.springframework.security.oauth2.server.resource.web.authentication.BearerTokenAuthenticationFilter;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.web.cors.CorsConfigurationSource;
import org.springframework.web.cors.UrlBasedCorsConfigurationSource;

import java.util.List;
import java.util.ArrayList;

@Configuration
@EnableWebSecurity
@EnableMethodSecurity
@RequiredArgsConstructor
public class SecurityConfig {

    private final StringRedisTemplate redisTemplate;
    private final ObjectMapper objectMapper;

    @Bean
    public SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
        http
                .csrf(AbstractHttpConfigurer::disable)
                .cors(Customizer.withDefaults())
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers("/actuator/health", "/actuator/info", "/.well-known/jwks.json").permitAll()
                        .requestMatchers(org.springframework.http.HttpMethod.GET, "/api/v1/storage/files/**").permitAll()
                        .requestMatchers("/api/v1/knowposts/feed", "/api/v1/knowposts/feed/recommend").permitAll()
                        .requestMatchers(org.springframework.http.HttpMethod.POST, "/api/v1/knowposts/ai-drafts").permitAll()
                        .requestMatchers("/api/v1/assistant-tools/**").permitAll()
                        .requestMatchers(
                                org.springframework.http.HttpMethod.POST,
                                "/api/v1/internal/moderation/tasks/*/result"
                        ).permitAll()
                        .requestMatchers("/api/v1/internal/moderation/**").permitAll()
                        .requestMatchers(org.springframework.http.HttpMethod.GET, "/api/v1/knowposts/detail/*").permitAll()
                        .requestMatchers(org.springframework.http.HttpMethod.GET, "/api/v1/comments", "/api/v1/comments/hot").permitAll()
                        .requestMatchers("/api/v1/admin/**").hasRole("ADMIN")
                        .requestMatchers(
                                org.springframework.http.HttpMethod.POST,
                                "/api/v1/knowposts/**"
                        ).hasRole("USER")
                        .requestMatchers(
                                org.springframework.http.HttpMethod.PUT,
                                "/api/v1/knowposts/**"
                        ).hasRole("USER")
                        .requestMatchers(
                                org.springframework.http.HttpMethod.PATCH,
                                "/api/v1/knowposts/**"
                        ).hasRole("USER")
                        .requestMatchers(
                                org.springframework.http.HttpMethod.DELETE,
                                "/api/v1/knowposts/**"
                        ).hasRole("USER")
                        .requestMatchers(
                                "/api/v1/auth/send-code",
                                "/api/v1/auth/register",
                                "/api/v1/auth/login",
                                "/api/v1/auth/token/refresh",
                                "/api/v1/auth/logout",
                                "/api/v1/auth/password/reset"
                        ).permitAll()
                        .anyRequest().authenticated()
                )
                .oauth2ResourceServer(oauth -> oauth.jwt(jwt -> jwt.jwtAuthenticationConverter(jwtAuthenticationConverter())));

        // ===================== 修复：正确位置 + 正确注入 =====================
        http.addFilterAfter(
                new JwtRedisCheckFilter(redisTemplate, objectMapper),
                BearerTokenAuthenticationFilter.class
        );

        return http.build();
    }

    @Bean
    public Converter<Jwt, AbstractAuthenticationToken> jwtAuthenticationConverter() {
        JwtAuthenticationConverter converter = new JwtAuthenticationConverter();
        converter.setJwtGrantedAuthoritiesConverter(jwt -> {
            String role = String.valueOf(jwt.getClaims().getOrDefault("role", "USER"));
            List<GrantedAuthority> authorities = new ArrayList<>();
            authorities.add(new SimpleGrantedAuthority("ROLE_" + role));
            return authorities;
        });
        return converter;
    }

    @Bean
    public CorsConfigurationSource corsConfigurationSource() {
        CorsConfiguration configuration = new CorsConfiguration();
        configuration.setAllowedOrigins(List.of("*"));
        configuration.setAllowedMethods(List.of("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"));
        configuration.setAllowedHeaders(List.of(
                "Authorization", "Content-Type", "X-Requested-With",
                "X-Moderation-Service-Secret",
                "X-Creator-Handoff-Secret", "X-Assistant-Service-Secret", "X-Assistant-Capability",
                "Idempotency-Key", "X-Zhiguang-Service",
                "X-Zhiguang-User-Id", "X-Zhiguang-Roles", "X-Trace-Id",
                "X-Zhiguang-Timestamp", "X-Zhiguang-Nonce", "X-Zhiguang-Signature"
        ));
        configuration.setExposedHeaders(List.of("ETag", "X-Trace-ID"));
        configuration.setAllowCredentials(false);
        UrlBasedCorsConfigurationSource source = new UrlBasedCorsConfigurationSource();
        source.registerCorsConfiguration("/**", configuration);
        return source;
    }
}
