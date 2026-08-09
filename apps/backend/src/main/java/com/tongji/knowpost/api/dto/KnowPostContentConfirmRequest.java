package com.tongji.knowpost.api.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

/**
 * 内容上传确认请求。
 */
public record KnowPostContentConfirmRequest(
        @NotBlank(message = "正文对象键不能为空") String objectKey,
        @NotBlank(message = "正文上传凭证 ETag 不能为空") String etag,
        @NotNull(message = "正文大小不能为空") Long size,
        @NotBlank(message = "正文校验值不能为空") String sha256
) {}
