package com.tongji.common.exception;

import lombok.Getter;

@Getter
public enum ErrorCode {
    IDENTIFIER_EXISTS("IDENTIFIER_EXISTS", "Identifier already exists"),
    IDENTIFIER_NOT_FOUND("IDENTIFIER_NOT_FOUND", "Identifier not found"),
    ZGID_EXISTS("ZGID_EXISTS", "Account already exists"),
    VERIFICATION_RATE_LIMIT("VERIFICATION_RATE_LIMIT", "Verification rate limit exceeded"),
    VERIFICATION_DAILY_LIMIT("VERIFICATION_DAILY_LIMIT", "Verification daily limit exceeded"),
    VERIFICATION_NOT_FOUND("VERIFICATION_NOT_FOUND", "Verification code not found or expired"),
    VERIFICATION_MISMATCH("VERIFICATION_MISMATCH", "Verification code is invalid"),
    VERIFICATION_TOO_MANY_ATTEMPTS("VERIFICATION_TOO_MANY_ATTEMPTS", "Too many verification attempts"),
    INVALID_CREDENTIALS("INVALID_CREDENTIALS", "Invalid credentials"),
    PASSWORD_POLICY_VIOLATION("PASSWORD_POLICY_VIOLATION", "Password policy violation"),
    TERMS_NOT_ACCEPTED("TERMS_NOT_ACCEPTED", "Terms must be accepted"),
    REFRESH_TOKEN_INVALID("REFRESH_TOKEN_INVALID", "Refresh token is invalid"),
    BAD_REQUEST("BAD_REQUEST", "Request parameters are invalid"),
    VALIDATION_ERROR("VALIDATION_ERROR", "Request validation failed"),
    FIELD_TOO_LONG("FIELD_TOO_LONG", "Field exceeds its contract length"),
    INVALID_DRAFT_METADATA("INVALID_DRAFT_METADATA", "Draft metadata is invalid"),
    AUTHENTICATION_REQUIRED("AUTHENTICATION_REQUIRED", "Authentication is required"),
    UNAUTHORIZED("UNAUTHORIZED", "Unauthorized"),
    FORBIDDEN("FORBIDDEN", "Forbidden"),
    NOT_FOUND("NOT_FOUND", "Resource not found"),
    CONFLICT("CONFLICT", "Resource conflict"),
    IDEMPOTENCY_CONFLICT("IDEMPOTENCY_CONFLICT", "Idempotency conflict"),
    BUSINESS_REJECTED("BUSINESS_REJECTED", "Business rule rejected the request"),
    DEPENDENCY_UNAVAILABLE("DEPENDENCY_UNAVAILABLE", "Dependency unavailable"),
    RESULT_UNKNOWN("RESULT_UNKNOWN", "Result is unknown"),
    INTERNAL_ERROR("INTERNAL_ERROR", "Internal server error");

    private final String code;
    private final String defaultMessage;

    ErrorCode(String code, String defaultMessage) {
        this.code = code;
        this.defaultMessage = defaultMessage;
    }
}
