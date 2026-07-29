package com.tongji.moderation.api;

import com.fasterxml.jackson.databind.node.NullNode;
import com.tongji.moderation.ModerationCommunityContextService;
import com.tongji.moderation.api.dto.ModerationCommunityContentRecord;
import com.tongji.moderation.api.dto.ModerationCommunityContentSnapshot;
import com.tongji.moderation.api.dto.ModerationReportEvidence;
import com.tongji.moderation.api.dto.ModerationViolationRecord;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;

@RestController
@RequestMapping("/api/v1/internal/moderation")
public class ModerationCommunityController {
    private static final String SECRET_HEADER = "X-Moderation-Service-Secret";

    private final ModerationCommunityContextService service;
    private final String authSecret;

    public ModerationCommunityController(
            ModerationCommunityContextService service,
            @Value("${moderation-agent.auth-secret:}") String authSecret
    ) {
        this.service = service;
        this.authSecret = authSecret == null ? "" : authSecret;
    }

    @GetMapping("/contents/{contentId}/context")
    public ModerationCommunityContentSnapshot getContentContext(
            @PathVariable long contentId,
            @RequestHeader(value = SECRET_HEADER, required = false) String suppliedSecret
    ) {
        requireSecret(suppliedSecret);
        return service.getContentContext(contentId);
    }

    @GetMapping("/contents/{contentId}/parent")
    public Object getParentComment(
            @PathVariable long contentId,
            @RequestHeader(value = SECRET_HEADER, required = false) String suppliedSecret
    ) {
        requireSecret(suppliedSecret);
        ModerationCommunityContentRecord parent = service.getParentComment(contentId);
        return parent == null ? NullNode.getInstance() : parent;
    }

    @GetMapping("/contents/{contentId}/conversation")
    public List<ModerationCommunityContentRecord> getConversationContext(
            @PathVariable long contentId,
            @RequestParam(defaultValue = "10") int limit,
            @RequestHeader(value = SECRET_HEADER, required = false) String suppliedSecret
    ) {
        requireSecret(suppliedSecret);
        return service.getConversationContext(contentId, limit);
    }

    @GetMapping("/authors/{authorId}/contents")
    public List<ModerationCommunityContentRecord> getAuthorRecentContents(
            @PathVariable long authorId,
            @RequestParam(defaultValue = "5") int limit,
            @RequestHeader(value = SECRET_HEADER, required = false) String suppliedSecret
    ) {
        requireSecret(suppliedSecret);
        return service.getAuthorRecentContents(authorId, limit);
    }

    @GetMapping("/authors/{authorId}/violations")
    public List<ModerationViolationRecord> getAuthorViolationHistory(
            @PathVariable long authorId,
            @RequestHeader(value = SECRET_HEADER, required = false) String suppliedSecret
    ) {
        requireSecret(suppliedSecret);
        return service.getAuthorViolationHistory(authorId);
    }

    @GetMapping("/contents/{contentId}/reports")
    public List<ModerationReportEvidence> getContentReports(
            @PathVariable long contentId,
            @RequestHeader(value = SECRET_HEADER, required = false) String suppliedSecret
    ) {
        requireSecret(suppliedSecret);
        return service.getContentReports(contentId);
    }

    private void requireSecret(String suppliedSecret) {
        if (authSecret.isBlank() || suppliedSecret == null || suppliedSecret.isBlank()
                || !MessageDigest.isEqual(
                        authSecret.getBytes(StandardCharsets.UTF_8),
                        suppliedSecret.getBytes(StandardCharsets.UTF_8))) {
            throw new AccessDeniedException("审核服务凭证无效");
        }
    }
}
