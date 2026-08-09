package com.tongji.common.exception;

import lombok.Getter;

@Getter
public enum ErrorCode {
    IDENTIFIER_EXISTS("IDENTIFIER_EXISTS", "账号已存在"),
    IDENTIFIER_NOT_FOUND("IDENTIFIER_NOT_FOUND", "账号不存在"),
    ZGID_EXISTS("ZGID_EXISTS", "知光号已存在"),
    VERIFICATION_RATE_LIMIT("VERIFICATION_RATE_LIMIT", "验证码发送过于频繁"),
    VERIFICATION_DAILY_LIMIT("VERIFICATION_DAILY_LIMIT", "验证码发送次数超限"),
    VERIFICATION_NOT_FOUND("VERIFICATION_NOT_FOUND", "验证码不存在或已过期"),
    VERIFICATION_MISMATCH("VERIFICATION_MISMATCH", "验证码错误"),
    VERIFICATION_TOO_MANY_ATTEMPTS("VERIFICATION_TOO_MANY_ATTEMPTS", "验证码尝试次数过多"),
    INVALID_CREDENTIALS("INVALID_CREDENTIALS", "登录凭证错误"),
    PASSWORD_POLICY_VIOLATION("PASSWORD_POLICY_VIOLATION", "密码强度不足"),
    TERMS_NOT_ACCEPTED("TERMS_NOT_ACCEPTED", "请先同意服务条款"),
    REFRESH_TOKEN_INVALID("REFRESH_TOKEN_INVALID", "刷新令牌无效"),
    BAD_REQUEST("BAD_REQUEST", "请求参数错误"),
    VALIDATION_ERROR("VALIDATION_ERROR", "参数校验失败"),
    AUTHENTICATION_REQUIRED("AUTHENTICATION_REQUIRED", "需要登录"),
    UNAUTHORIZED("UNAUTHORIZED", "未授权"),
    FORBIDDEN("FORBIDDEN", "无权执行该操作"),
    NOT_FOUND("NOT_FOUND", "资源不存在"),
    CONFLICT("CONFLICT", "资源冲突"),
    IDEMPOTENCY_CONFLICT("IDEMPOTENCY_CONFLICT", "幂等冲突"),
    BUSINESS_REJECTED("BUSINESS_REJECTED", "业务规则拒绝"),
    DEPENDENCY_UNAVAILABLE("DEPENDENCY_UNAVAILABLE", "依赖服务不可用"),
    RESULT_UNKNOWN("RESULT_UNKNOWN", "结果未知"),
    INTERNAL_ERROR("INTERNAL_ERROR", "服务器内部错误");

    private final String code;
    private final String defaultMessage;

    ErrorCode(String code, String defaultMessage) {
        this.code = code;
        this.defaultMessage = defaultMessage;
    }
}
