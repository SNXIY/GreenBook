package com.tongji.agentfacade.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.agentfacade.api.dto.AgentErrorResponse;
import com.tongji.agentfacade.mapper.AgentIdempotencyMapper;
import com.tongji.agentfacade.mapper.AgentIdempotencyRecord;
import com.tongji.common.exception.BusinessException;
import com.tongji.knowpost.id.SnowflakeIdGenerator;
import lombok.RequiredArgsConstructor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.HexFormat;
import java.util.function.Supplier;

@Service
@RequiredArgsConstructor
public class IdempotencyService {

    private static final Logger log = LoggerFactory.getLogger(IdempotencyService.class);
    private static final int DEFAULT_TTL_HOURS = 24;
    private static final int MAX_RESPONSE_BODY_LENGTH = 64 * 1024; // 64KB

    private final AgentIdempotencyMapper mapper;
    private final SnowflakeIdGenerator idGen;
    private final ObjectMapper objectMapper;

    /**
     * Execute an operation with idempotency guarantee.
     * Returns the stored result if the key was already used, otherwise executes the operation.
     */
    @Transactional
    public <T> T execute(long userId, String operation, String idempotencyKey, String requestBody,
                         Class<T> resultType, Supplier<T> action) {
        String requestHash = sha256(requestBody);
        AgentIdempotencyRecord existing = mapper.findByUserOpKey(userId, operation, idempotencyKey);

        if (existing != null) {
            return handleExisting(existing, requestHash, operation, resultType);
        }

        long id = idGen.nextId();
        mapper.insert(id, userId, operation, idempotencyKey, requestHash,
                "IN_PROGRESS", null, null, null, null,
                Instant.now().plusSeconds(DEFAULT_TTL_HOURS * 3600));

        try {
            T result = action.get();
            String responseBody;
            try {
                responseBody = objectMapper.writeValueAsString(result);
            } catch (JsonProcessingException je) {
                responseBody = "{}";
            }
            if (responseBody.length() > MAX_RESPONSE_BODY_LENGTH) {
                log.warn("Response body truncated for idempotency: userId={}, op={}, len={}",
                        userId, operation, responseBody.length());
                responseBody = responseBody.substring(0, MAX_RESPONSE_BODY_LENGTH);
            }
            mapper.complete(id, 200, responseBody, null, null, "COMPLETED");
            log.info("Idempotency completed: userId={}, operation={}, key={}", userId, operation, idempotencyKey.substring(0, 8));
            return result;
        } catch (BusinessException e) {
            String errorBody;
            try {
                errorBody = objectMapper.writeValueAsString(
                        new AgentErrorResponse(e.getErrorCode().getCode(), e.getMessage(),
                                e.getMessage(), false, false, ""));
            } catch (JsonProcessingException je) {
                errorBody = "{}";
            }
            // Mark FAILED — the business rule rejected the request deterministically.
            // On replay with the same key+hash, the same error is returned.
            mapper.complete(id, 400, errorBody, null, null, "FAILED");
            throw e;
        } catch (Exception e) {
            String errorBody;
            try {
                errorBody = objectMapper.writeValueAsString(
                        new AgentErrorResponse("INTERNAL_ERROR", e.getMessage(),
                                "内部错误", true, false, ""));
            } catch (JsonProcessingException je) {
                errorBody = "{}";
            }
            // Infrastructure errors are retryable — mark FAILED so the caller can retry
            // with a different idempotency key if needed.
            mapper.complete(id, 500, errorBody, null, null, "FAILED");
            throw e;
        }
    }

    private <T> T handleExisting(AgentIdempotencyRecord existing, String requestHash, String operation,
                                 Class<T> resultType) {
        if ("IN_PROGRESS".equals(existing.getStatus())) {
            throw new BusinessException(
                    com.tongji.common.exception.ErrorCode.BAD_REQUEST,
                    "操作正在处理中，请稍后重试");
        }

        if (!requestHash.equals(existing.getRequestHash())) {
            String errorBody;
            try {
                errorBody = objectMapper.writeValueAsString(
                        new AgentErrorResponse("IDEMPOTENCY_CONFLICT",
                                "相同幂等键但请求内容不同",
                                "请求冲突，请使用不同的 Idempotency-Key",
                                false, false, ""));
            } catch (JsonProcessingException e) {
                errorBody = "{}";
            }
            throw new com.tongji.common.exception.BusinessException(
                    com.tongji.common.exception.ErrorCode.BAD_REQUEST, "请求冲突");
        }

        log.info("Idempotency replay: userId={}, operation={}, key={}",
                existing.getUserId(), operation, existing.getIdempotencyKey().substring(0, 8));

        if (existing.getResponseStatus() != null && existing.getResponseStatus() >= 400) {
            throw new BusinessException(
                    com.tongji.common.exception.ErrorCode.BAD_REQUEST,
                    "请求已失败，请勿使用相同 Idempotency-Key 重放");
        }

        if ("FAILED".equals(existing.getStatus())) {
            // The original request failed without recording a response body,
            // or the body is not directly deserializable — require a fresh key.
            throw new BusinessException(
                    com.tongji.common.exception.ErrorCode.BAD_REQUEST,
                    "请求已失败（无可恢复结果），请使用新的 Idempotency-Key 重试");
        }

        if (resultType == Void.class) {
            return null;
        }

        if (existing.getResponseBody() != null) {
            try {
                return objectMapper.readValue(existing.getResponseBody(), resultType);
            } catch (JsonProcessingException e) {
                throw new BusinessException(
                        com.tongji.common.exception.ErrorCode.INTERNAL_ERROR,
                        "无法恢复幂等结果");
            }
        }

        throw new BusinessException(
                com.tongji.common.exception.ErrorCode.INTERNAL_ERROR,
                "幂等记录异常");
    }

    @Scheduled(fixedDelayString = "${agent.idempotency.cleanup-delay-ms:3600000}",
            initialDelayString = "${agent.idempotency.cleanup-initial-delay-ms:300000}")
    public void cleanupExpiredRecords() {
        int deleted = mapper.deleteExpired(Instant.now());
        if (deleted > 0) {
            log.info("Cleaned up {} expired idempotency records", deleted);
        }
    }

    public static String sha256(String input) {
        if (input == null) {
            input = "";
        }
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(input.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException e) {
            throw new RuntimeException("SHA-256 not available", e);
        }
    }
}
