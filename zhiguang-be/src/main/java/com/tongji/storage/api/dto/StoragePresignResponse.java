package com.tongji.storage.api.dto;

import java.util.Map;

/**
 * 预签名直传响应。
 */
public record StoragePresignResponse(
        String objectKey,
        String putUrl,
        String publicUrl,
        Map<String, String> headers,
        int expiresIn
) {}
