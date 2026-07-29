package com.tongji.moderation.api;

import com.tongji.knowpost.service.KnowPostService;
import jakarta.validation.Valid;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

@RestController
@RequestMapping("/api/v1/internal/moderation/tasks")
public class ModerationResultController {

    private final KnowPostService knowPostService;
    private final String authSecret;

    public ModerationResultController(
            KnowPostService knowPostService,
            @Value("${moderation-agent.auth-secret:}") String authSecret
    ) {
        this.knowPostService = knowPostService;
        this.authSecret = authSecret == null ? "" : authSecret;
    }

    @PostMapping("/{taskId}/result")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void applyResult(
            @PathVariable String taskId,
            @RequestHeader(
                    value = "X-Moderation-Service-Secret",
                    required = false
            ) String providedSecret,
            @Valid @RequestBody ModerationResultRequest request
    ) {
        if (authSecret.isBlank() || providedSecret == null
                || !MessageDigest.isEqual(
                authSecret.getBytes(StandardCharsets.UTF_8),
                providedSecret.getBytes(StandardCharsets.UTF_8)
        )) {
            throw new AccessDeniedException("审核回调凭证无效");
        }
        knowPostService.applyModerationResult(
                taskId,
                request.contentId(),
                request.status(),
                request.finalAction(),
                request.reason()
        );
    }
}
