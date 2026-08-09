package com.tongji.storage;

import com.aliyun.oss.OSS;
import com.aliyun.oss.OSSClientBuilder;
import com.aliyun.oss.model.PutObjectRequest;
import com.aliyun.oss.model.OSSObject;
import com.aliyun.oss.HttpMethod;
import com.aliyun.oss.model.GeneratePresignedUrlRequest;
import com.tongji.storage.config.OssProperties;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.Date;

@Service
@RequiredArgsConstructor
public class OssStorageService {

    private final OssProperties props;

    public String uploadAvatar(long userId, MultipartFile file) {
        String original = file.getOriginalFilename();
        String ext = "";
        if (original != null && original.contains(".")) {
            ext = original.substring(original.lastIndexOf('.'));
        }
        String objectKey = props.getFolder() + "/" + userId + "-" + Instant.now().toEpochMilli() + ext;

        if (isLocal()) {
            try {
                writeLocal(objectKey, file.getBytes());
                return publicUrl(objectKey);
            } catch (IOException e) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "头像文件读取失败");
            }
        }
        ensureConfigured();

        OSS client = new OSSClientBuilder().build(props.getEndpoint(), props.getAccessKeyId(), props.getAccessKeySecret());

        try {
            PutObjectRequest request = new PutObjectRequest(props.getBucket(), objectKey, file.getInputStream());
            client.putObject(request);
        } catch (IOException e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "头像文件读取失败");
        } finally {
            client.shutdown();
        }

        return publicUrl(objectKey);
    }

    private String publicUrl(String objectKey) {
        if (isLocal() || !isOssConfigured()) {
            return props.getLocalPublicBaseUrl().replaceAll("/$", "")
                    + "/api/v1/storage/files/" + encodePath(objectKey);
        }
        if (props.getPublicDomain() != null && !props.getPublicDomain().isBlank()) {
            return props.getPublicDomain().replaceAll("/$", "") + "/" + objectKey;
        }
        return "https://" + props.getBucket() + "." + props.getEndpoint() + "/" + objectKey;
    }

    public String generatePresignedPutUrl(String objectKey, String contentType, int expiresInSeconds) {
        if (isLocal()) {
            return props.getLocalPublicBaseUrl().replaceAll("/$", "")
                    + "/api/v1/storage/local-upload?objectKey="
                    + URLEncoder.encode(objectKey, StandardCharsets.UTF_8);
        }
        ensureConfigured();
        OSS client = new OSSClientBuilder().build(props.getEndpoint(), props.getAccessKeyId(), props.getAccessKeySecret());
        try {
            Date expiration = new Date(System.currentTimeMillis() + expiresInSeconds * 1000L);
            GeneratePresignedUrlRequest request = new GeneratePresignedUrlRequest(props.getBucket(), objectKey, HttpMethod.PUT);
            request.setExpiration(expiration);
            if (contentType != null && !contentType.isBlank()) {
                request.setContentType(contentType);
            }
            URL url = client.generatePresignedUrl(request);
            return url.toString();
        } finally {
            client.shutdown();
        }
    }

    /**
     * Store text content. When OSS is not configured, text is stored on the local
     * filesystem regardless of the provider setting. This keeps pure-text posts
     * working without any object-storage infrastructure.
     */
    public String putTextObject(String objectKey, String content, String contentType) {
        if (objectKey == null || objectKey.isBlank()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "对象键无效");
        }
        byte[] bytes = content == null ? new byte[0] : content.getBytes(StandardCharsets.UTF_8);
        if (isLocal() || !isOssConfigured()) {
            writeLocal(objectKey, bytes);
            return sha256(bytes);
        }
        OSS client = new OSSClientBuilder().build(props.getEndpoint(), props.getAccessKeyId(), props.getAccessKeySecret());
        try (InputStream input = new java.io.ByteArrayInputStream(bytes)) {
            PutObjectRequest request = new PutObjectRequest(props.getBucket(), objectKey, input);
            var meta = new com.aliyun.oss.model.ObjectMetadata();
            meta.setContentLength(bytes.length);
            if (contentType != null && !contentType.isBlank()) {
                meta.setContentType(contentType);
            }
            request.setMetadata(meta);
            var result = client.putObject(request);
            return result.getETag() == null ? "" : result.getETag();
        } catch (BusinessException e) {
            throw e;
        } catch (Exception e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "正文上传失败");
        } finally {
            client.shutdown();
        }
    }

    public String publicObjectUrl(String objectKey) {
        return publicUrl(objectKey);
    }

    /**
     * Read text content. When OSS is not configured, reads from the local
     * filesystem regardless of the provider setting.
     */
    public String readTextObject(String objectKey, int maxBytes) {
        if (objectKey == null || objectKey.isBlank()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "正文文件不存在");
        }
        int limit = Math.max(1, maxBytes);
        if (isLocal() || !isOssConfigured()) {
            return readLocalText(objectKey, limit);
        }
        OSS client = new OSSClientBuilder().build(props.getEndpoint(), props.getAccessKeyId(), props.getAccessKeySecret());
        try (OSSObject object = client.getObject(props.getBucket(), objectKey);
             InputStream input = object.getObjectContent()) {
            byte[] bytes = input.readNBytes(limit + 1);
            if (bytes.length > limit) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "正文文件过大");
            }
            return new String(bytes, StandardCharsets.UTF_8);
        } catch (BusinessException e) {
            throw e;
        } catch (Exception e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "正文文件读取失败");
        } finally {
            client.shutdown();
        }
    }

    private String readLocalText(String objectKey, int limit) {
        try {
            byte[] bytes = Files.readAllBytes(resolveLocal(objectKey));
            if (bytes.length > limit) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "正文文件过大");
            }
            return new String(bytes, StandardCharsets.UTF_8);
        } catch (BusinessException e) {
            throw e;
        } catch (Exception e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "正文文件读取失败");
        }
    }

    private boolean isOssConfigured() {
        return !isBlank(props.getEndpoint())
                && !isBlank(props.getAccessKeyId())
                && !isBlank(props.getAccessKeySecret())
                && !isBlank(props.getBucket());
    }

    private void ensureConfigured() {
        if (isLocal()) {
            return;
        }
        if (!isOssConfigured()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "对象存储未配置");
        }
    }

    public boolean isLocal() {
        return "local".equalsIgnoreCase(props.getProvider());
    }

    public String putLocalObject(String objectKey, byte[] bytes) {
        if (!isLocal()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "本地存储未启用");
        }
        byte[] content = bytes == null ? new byte[0] : bytes;
        writeLocal(objectKey, content);
        return sha256(content);
    }

    public byte[] readLocalObject(String objectKey) {
        if (!isLocal()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "本地存储未启用");
        }
        try {
            return Files.readAllBytes(resolveLocal(objectKey));
        } catch (IOException e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "文件不存在");
        }
    }

    private void writeLocal(String objectKey, byte[] bytes) {
        try {
            Path target = resolveLocal(objectKey);
            Files.createDirectories(target.getParent());
            Files.write(target, bytes, StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
        } catch (IOException e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "本地文件写入失败");
        }
    }

    private Path resolveLocal(String objectKey) {
        if (objectKey == null || objectKey.isBlank() || objectKey.contains("\0")) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "对象键无效");
        }
        Path root = Path.of(props.getLocalRoot()).toAbsolutePath().normalize();
        Path target = root.resolve(objectKey.replace('\\', '/')).normalize();
        if (!target.startsWith(root)) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "对象键无效");
        }
        return target;
    }

    private String encodePath(String objectKey) {
        return java.util.Arrays.stream(objectKey.replace('\\', '/').split("/"))
                .map(part -> URLEncoder.encode(part, StandardCharsets.UTF_8).replace("+", "%20"))
                .collect(java.util.stream.Collectors.joining("/"));
    }

    private String sha256(byte[] bytes) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256").digest(bytes);
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            return "";
        }
    }

    private boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
