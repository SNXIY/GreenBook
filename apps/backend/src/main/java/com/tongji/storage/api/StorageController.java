package com.tongji.storage.api;

import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.auth.token.JwtService;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.storage.OssStorageService;
import com.tongji.storage.api.dto.StoragePresignRequest;
import com.tongji.storage.api.dto.StoragePresignResponse;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.CacheControl;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import jakarta.servlet.http.HttpServletRequest;

import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.Map;
import java.util.UUID;

@RestController
@RequestMapping("/api/v1/storage")
@Validated
@RequiredArgsConstructor
public class StorageController {

    private final OssStorageService ossStorageService;
    private final JwtService jwtService;
    private final KnowPostMapper knowPostMapper;

    /**
     * 获取用于直传的 PUT 预签名 URL。
     */
    @PostMapping("/presign")
    public StoragePresignResponse presign(@Valid @RequestBody StoragePresignRequest request,
                                          @AuthenticationPrincipal Jwt jwt) {
        long userId = jwtService.extractUserId(jwt);

        long postId;
        try {
            postId = Long.parseLong(request.postId());
        } catch (NumberFormatException e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "postId 非法");
        }

        // 权限校验：postId 必须属于当前用户
        KnowPost post = knowPostMapper.findById(postId);
        if (post == null || post.getCreatorId() == null || post.getCreatorId() != userId) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }

        String scene = request.scene();
        String objectKey;
        String ext = normalizeExt(request.ext(), request.contentType(), scene);

        if ("knowpost_content".equals(scene)) {
            objectKey = "posts/" + postId + "/content" + ext;
        } else if ("knowpost_image".equals(scene)) {
            String date = DateTimeFormatter.ofPattern("yyyyMMdd").withZone(ZoneId.of("UTC")).format(Instant.now());
            String rand = UUID.randomUUID().toString().replaceAll("-", "").substring(0, 8);
            objectKey = "posts/" + postId + "/images/" + date + "/" + rand + ext;
        } else {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "不支持的上传场景");
        }

        int expiresIn = 600; // 10 分钟
        String putUrl = ossStorageService.generatePresignedPutUrl(objectKey, request.contentType(), expiresIn);
        String publicUrl = ossStorageService.publicObjectUrl(objectKey);
        Map<String, String> headers = Map.of("Content-Type", request.contentType());
        return new StoragePresignResponse(objectKey, putUrl, publicUrl, headers, expiresIn);
    }

    @PutMapping("/local-upload")
    public ResponseEntity<Void> localUpload(@RequestParam("objectKey") String objectKey,
                                            @RequestBody byte[] content,
                                            @AuthenticationPrincipal Jwt jwt) {
        long userId = jwtService.extractUserId(jwt);
        verifyObjectOwnership(objectKey, userId);
        String etag = ossStorageService.putLocalObject(objectKey, content);
        return ResponseEntity.noContent()
                .eTag(etag)
                .build();
    }

    @GetMapping("/files/**")
    public ResponseEntity<byte[]> localFile(HttpServletRequest request) {
        String prefix = "/api/v1/storage/files/";
        String path = request.getRequestURI();
        if (!path.startsWith(prefix) || path.length() <= prefix.length()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "文件路径无效");
        }
        String objectKey = java.net.URLDecoder.decode(
                path.substring(prefix.length()), java.nio.charset.StandardCharsets.UTF_8);
        byte[] content = ossStorageService.readLocalObject(objectKey);
        String detected;
        try {
            detected = java.nio.file.Files.probeContentType(java.nio.file.Path.of(objectKey));
        } catch (Exception ignored) {
            detected = null;
        }
        MediaType type = detected == null
                ? MediaType.APPLICATION_OCTET_STREAM
                : MediaType.parseMediaType(detected);
        return ResponseEntity.ok()
                .cacheControl(CacheControl.noCache())
                .contentType(type)
                .body(content);
    }

    private void verifyObjectOwnership(String objectKey, long userId) {
        String normalized = objectKey == null ? "" : objectKey.replace('\\', '/');
        String[] parts = normalized.split("/");
        if (parts.length < 3 || !"posts".equals(parts[0])) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "对象键无效");
        }
        long postId;
        try {
            postId = Long.parseLong(parts[1]);
        } catch (NumberFormatException e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "对象键无效");
        }
        KnowPost post = knowPostMapper.findById(postId);
        if (post == null || post.getCreatorId() == null || post.getCreatorId() != userId) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或无权限");
        }
    }

    private String normalizeExt(String ext, String contentType, String scene) {
        if (ext != null && !ext.isBlank()) {
            return ext.startsWith(".") ? ext : "." + ext;
        }
        if ("knowpost_content".equals(scene)) {
            return switch (contentType) {
                case "text/markdown" -> ".md";
                case "text/html" -> ".html";
                case "text/plain" -> ".txt";
                case "application/json" -> ".json";
                default -> ".bin";
            };
        } else {
            return switch (contentType) {
                case "image/jpeg" -> ".jpg";
                case "image/png" -> ".png";
                case "image/webp" -> ".webp";
                default -> ".img";
            };
        }
    }
}
