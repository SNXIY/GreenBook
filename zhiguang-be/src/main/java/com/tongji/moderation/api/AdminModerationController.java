package com.tongji.moderation.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.tongji.auth.token.JwtService;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.moderation.ModerationAgentClient;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/admin/moderation")
@RequiredArgsConstructor
@PreAuthorize("hasRole('ADMIN')")
public class AdminModerationController {

    private final ModerationAgentClient moderationAgentClient;
    private final JwtService jwtService;
    private final KnowPostService knowPostService;

    @GetMapping("/statistics")
    public JsonNode statistics() {
        return moderationAgentClient.getStatistics();
    }

    @GetMapping("/tasks")
    public JsonNode tasks(
            @RequestParam(required = false) String status,
            @RequestParam(defaultValue = "50") int limit,
            @RequestParam(defaultValue = "0") int offset
    ) {
        return moderationAgentClient.listTasks(status, limit, offset);
    }

    @GetMapping("/callbacks")
    public JsonNode callbacks(
            @RequestParam(required = false) String status,
            @RequestParam(defaultValue = "50") int limit,
            @RequestParam(defaultValue = "0") int offset
    ) {
        return moderationAgentClient.listCallbacks(status, limit, offset);
    }

    @GetMapping("/tasks/{taskId}")
    public JsonNode task(@PathVariable String taskId) {
        return moderationAgentClient.getTaskJson(taskId);
    }

    @PostMapping("/tasks/{taskId}/review")
    public JsonNode review(
            @PathVariable String taskId,
            @Valid @RequestBody AdminModerationReviewRequest request,
            @AuthenticationPrincipal Jwt jwt
    ) {
        String reviewerId = "zhiguang-admin:" + jwtService.extractUserId(jwt);
        JsonNode result = moderationAgentClient.submitHumanReview(
                taskId,
                reviewerId,
                request.action(),
                request.riskType(),
                request.comment(),
                request.expectedVersion()
        );
        JsonNode task = result.path("task");
        String reason = task.path("human_decision").path("comment").asText("");
        if (reason.isBlank()) {
            reason = task.path("agent_decision").path("reason").asText("");
        }
        knowPostService.applyModerationResult(
                taskId,
                task.path("content_id").asText(null),
                task.path("status").asText(""),
                task.path("final_action").asText(""),
                reason
        );
        return result;
    }
}
