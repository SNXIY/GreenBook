package com.tongji.auth.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import org.springframework.beans.factory.annotation.Value;
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

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

@Configuration
@EnableWebSecurity
@EnableMethodSecurity
@RequiredArgsConstructor
public class SecurityConfig {

    private final StringRedisTemplate redisTemplate;
    private final ObjectMapper objectMapper;

    @Value("${app.cors.allowed-origins:http://127.0.0.1:5173,http://localhost:5173}")
    private String allowedOrigins;

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
.requestMatchers(org.springframework.http.HttpMethod.GET, "/api/v1/knowposts/detail/*").permitAll()
                        .requestMatchers(org.springframework.http.HttpMethod.GET, "/api/v1/comments", "/api/v1/comments/hot").permitAll()
                        .requestMatchers("/api/v1/admin/**").hasRole("ADMIN")
                        // Agent Facade API: require authenticated USER role
                        .requestMatchers("/api/v1/agent/**").hasRole("USER")
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

        // Redis allow-list validation requires the authenticated JWT principal.
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
        configuration.setAllowedOrigins(
                Arrays.stream(allowedOrigins.split(","))
                        .map(String::trim)
                        .filter(value -> !value.isBlank())
                        .toList()
        );
        configuration.setAllowedMethods(List.of("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"));
        configuration.setAllowedHeaders(List.of(
                "Authorization", "Content-Type", "X-Requested-With",
                "Idempotency-Key", "X-Trace-ID", "X-Trace-Id",
                "X-Conversation-Id", "X-Agent-Run-Id", "X-Tool-Call-Id",
                "traceparent"
        ));
        configuration.setExposedHeaders(List.of("ETag", "X-Trace-ID", "X-Trace-Id"));
        configuration.setAllowCredentials(false);
        UrlBasedCorsConfigurationSource source = new UrlBasedCorsConfigurationSource();
        source.registerCorsConfiguration("/**", configuration);
        return source;
    }
}
