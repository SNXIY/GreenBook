package com.tongji.assistant.api;

import com.tongji.assistant.api.dto.AssistantPostContext;
import com.tongji.assistant.api.dto.AssistantPostSummary;
import com.tongji.assistant.api.dto.AssistantPublishRequest;
import com.tongji.assistant.api.dto.AssistantPublishResponse;
import com.tongji.assistant.api.dto.AssistantEngagementAnalytics;
import com.tongji.assistant.api.dto.AssistantCommentReplyRequest;
import com.tongji.assistant.api.dto.AssistantCapabilityRequest;
import com.tongji.assistant.api.dto.AssistantCapabilityResponse;
import com.tongji.assistant.api.dto.AssistantOwnPostSummary;
import com.tongji.assistant.api.dto.AssistantBatchDeleteRequest;
import com.tongji.assistant.api.dto.AssistantBatchDeleteResponse;
import com.tongji.assistant.service.AssistantCapabilityService;
import com.tongji.assistant.service.AssistantToolService;
import com.tongji.comment.api.dto.CommentResponse;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;

@RestController
@RequestMapping("/api/v1/assistant-tools")
@RequiredArgsConstructor
public class AssistantToolController {
    private final AssistantToolService service;
    private final AssistantCapabilityService capabilityService;

    @Value("${assistant.shared-secret:}")
    private String sharedSecret;

    @GetMapping("/posts/search")
    public List<AssistantPostSummary> search(
            @RequestParam("q") String query,
            @RequestParam(value = "limit", defaultValue = "5") int limit,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "X-Assistant-Capability", required = false) String capability
    ) {
        requireServiceSecret(supplied);
        capabilityService.authorize(
                requireCapability(capability),
                "community.search_posts",
                List.of()
        );
        return service.search(query, limit);
    }

    @PostMapping("/capabilities")
    public AssistantCapabilityResponse issueCapability(
            @Valid @RequestBody AssistantCapabilityRequest request,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "Authorization", required = false) String authorization
    ) {
        requireServiceSecret(supplied);
        return capabilityService.issue(bearerToken(authorization), request);
    }

    @DeleteMapping("/capabilities/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void revokeCapability(
            @PathVariable("id") String capabilityId,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "Authorization", required = false) String authorization
    ) {
        requireServiceSecret(supplied);
        capabilityService.revoke(bearerToken(authorization), capabilityId);
    }

    @GetMapping("/posts/{id}")
    public AssistantPostContext post(
            @PathVariable("id") long postId,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "X-Assistant-Capability", required = false) String capability
    ) {
        requireServiceSecret(supplied);
        capabilityService.authorize(
                requireCapability(capability),
                "community.get_post",
                List.of("post:" + postId)
        );
        return service.getPost(postId);
    }

    @GetMapping("/analytics/engagement")
    public AssistantEngagementAnalytics analyzeEngagement(
            @RequestParam(value = "topic", defaultValue = "") String topic,
            @RequestParam(value = "days", defaultValue = "7") int days,
            @RequestParam(value = "limit", defaultValue = "10") int limit,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "X-Assistant-Capability", required = false) String capability
    ) {
        requireServiceSecret(supplied);
        capabilityService.authorize(
                requireCapability(capability),
                "community.analyze_engagement",
                List.of()
        );
        return service.analyzeEngagement(topic, days, limit);
    }

    @GetMapping("/posts/{id}/draft-content")
    public AssistantPostContext ownDraft(
            @PathVariable("id") long postId,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "X-Assistant-Capability", required = false) String capability
    ) {
        requireServiceSecret(supplied);
        AssistantCapabilityService.CapabilityPrincipal principal = capabilityService.authorize(
                requireCapability(capability),
                "community.get_own_draft",
                List.of("post:" + postId)
        );
        return service.getOwnDraft(postId, principal.userId());
    }

    @GetMapping("/posts/mine")
    public List<AssistantOwnPostSummary> ownPosts(
            @RequestParam(value = "limit", defaultValue = "100") int limit,
            @RequestParam(value = "offset", defaultValue = "0") int offset,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "X-Assistant-Capability", required = false) String capability
    ) {
        requireServiceSecret(supplied);
        AssistantCapabilityService.CapabilityPrincipal principal = capabilityService.authorize(
                requireCapability(capability),
                "community.list_own_posts",
                List.of()
        );
        return service.listOwnPosts(principal.userId(), limit, offset);
    }

    @PostMapping("/posts/{id}/publish")
    public AssistantPublishResponse publish(
            @PathVariable("id") long postId,
            @Valid @RequestBody AssistantPublishRequest request,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "X-Assistant-Capability", required = false) String capability,
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey
    ) {
        requireServiceSecret(supplied);
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "缺少 Idempotency-Key");
        }
        AssistantCapabilityService.CapabilityPrincipal principal = capabilityService.authorize(
                requireCapability(capability),
                "publication.publish_now",
                List.of("post:" + postId)
        );
        if (principal.userId() != request.creatorId()) {
            throw new BusinessException(ErrorCode.FORBIDDEN, "能力令牌不能代替其他用户发布");
        }
        return service.publishAiDraft(
                postId,
                request.creatorId(),
                request.expectedContentSha256()
        );
    }

    @DeleteMapping("/posts/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void deleteOwnPost(
            @PathVariable("id") long postId,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "X-Assistant-Capability", required = false) String capability,
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey
    ) {
        requireServiceSecret(supplied);
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "缺少 Idempotency-Key");
        }
        AssistantCapabilityService.CapabilityPrincipal principal = capabilityService.authorize(
                requireCapability(capability),
                "community.delete_post",
                List.of("post:" + postId)
        );
        service.deleteOwnPost(postId, principal.userId());
    }

    @PostMapping("/posts/batch-delete")
    public AssistantBatchDeleteResponse deleteOwnPosts(
            @Valid @RequestBody AssistantBatchDeleteRequest request,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "X-Assistant-Capability", required = false) String capability,
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey
    ) {
        requireServiceSecret(supplied);
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "缺少 Idempotency-Key");
        }
        AssistantCapabilityService.CapabilityPrincipal principal = capabilityService.authorize(
                requireCapability(capability),
                "community.delete_own_posts_batch",
                request.postIds().stream().map(id -> "post:" + id).toList()
        );
        return service.deleteOwnPosts(request.postIds(), principal.userId());
    }

    @PostMapping("/comments/replies")
    public CommentResponse replyToComment(
            @Valid @RequestBody AssistantCommentReplyRequest request,
            @RequestHeader(value = "X-Assistant-Service-Secret", required = false) String supplied,
            @RequestHeader(value = "X-Assistant-Capability", required = false) String capability,
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey
    ) {
        requireServiceSecret(supplied);
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "缺少 Idempotency-Key");
        }
        AssistantCapabilityService.CapabilityPrincipal principal = capabilityService.authorize(
                requireCapability(capability),
                "community.reply_comment",
                List.of(
                        "post:" + request.postId(),
                        "comment:" + request.parentCommentId()
                )
        );
        if (!principal.runId().equals(request.assistantRunId())) {
            throw new BusinessException(ErrorCode.FORBIDDEN, "能力令牌与助手任务不一致");
        }
        return service.replyToComment(request);
    }

    private String requireCapability(String capability) {
        if (capability == null || capability.isBlank()) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "缺少助手能力令牌");
        }
        return capability.trim();
    }

    private String bearerToken(String authorization) {
        if (authorization == null || !authorization.regionMatches(
                true, 0, "Bearer ", 0, "Bearer ".length())) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "缺少 Bearer 令牌");
        }
        String token = authorization.substring("Bearer ".length()).trim();
        if (token.isBlank()) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "缺少 Bearer 令牌");
        }
        return token;
    }

    private void requireServiceSecret(String supplied) {
        if (sharedSecret == null || sharedSecret.isBlank()
                || supplied == null || supplied.isBlank()
                || !MessageDigest.isEqual(
                        sharedSecret.getBytes(StandardCharsets.UTF_8),
                        supplied.getBytes(StandardCharsets.UTF_8))) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "助手服务凭证无效");
        }
    }
}
