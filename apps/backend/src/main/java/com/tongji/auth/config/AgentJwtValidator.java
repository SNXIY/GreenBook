package com.tongji.auth.config;

import org.springframework.security.oauth2.core.OAuth2Error;
import org.springframework.security.oauth2.core.OAuth2TokenValidator;
import org.springframework.security.oauth2.core.OAuth2TokenValidatorResult;
import org.springframework.security.oauth2.jwt.Jwt;

import java.util.List;

/**
 * Validates that the JWT audience contains at least one of the required values.
 * Combined with issuer validation in the decoder builder, this ensures all
 * tokens are properly scoped before they reach controller logic.
 */
public class AgentJwtValidator implements OAuth2TokenValidator<Jwt> {

    private final List<String> requiredAudiences;

    public AgentJwtValidator(List<String> requiredAudiences) {
        this.requiredAudiences = List.copyOf(requiredAudiences);
    }

    @Override
    public OAuth2TokenValidatorResult validate(Jwt jwt) {
        List<String> audiences = jwt.getAudience();
        if (audiences == null || audiences.isEmpty()) {
            return OAuth2TokenValidatorResult.failure(
                    new OAuth2Error("invalid_token", "JWT 缺少 audience 声明", null));
        }
        for (String required : requiredAudiences) {
            if (audiences.contains(required)) {
                return OAuth2TokenValidatorResult.success();
            }
        }
        return OAuth2TokenValidatorResult.failure(
                new OAuth2Error("invalid_token",
                        "JWT audience 不满足要求，期望包含: " + String.join(", ", requiredAudiences),
                        null));
    }
}
