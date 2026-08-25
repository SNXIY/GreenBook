package com.tongji.agentfacade.api;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.agentfacade.api.dto.*;
import com.tongji.agentfacade.service.AgentFacadeService;
import com.tongji.agentfacade.service.IdempotencyService;
import com.tongji.agentfacade.service.ScheduledPublicationService;
import com.tongji.auth.token.JwtService;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.Parameter;
import io.swagger.v3.oas.annotations.enums.ParameterIn;
import io.swagger.v3.oas.annotations.media.Content;
import io.swagger.v3.oas.annotations.media.Schema;
import io.swagger.v3.oas.annotations.responses.ApiResponse;
import io.swagger.v3.oas.annotations.security.SecurityRequirement;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/v1/agent")
@RequiredArgsConstructor
@Tag(name = "Agent Facade", description = "Python Agent 稳定业务语义 API")
@SecurityRequirement(name = "bearerAuth")
public class AgentFacadeController {

    private final AgentFacadeService agentFacadeService;
    private final ScheduledPublicationService scheduledPublicationService;
    private final IdempotencyService idempotencyService;
    private final JwtService jwtService;
    private final ObjectMapper objectMapper;

    // ── Public Search ──────────────────────────────────────────────

    @GetMapping("/posts/search")
    @Operation(summary = "公共搜索帖子", description = "返回公开且允许当前用户查看的帖子，支持分页和排序")
    public SearchPageResponse searchPosts(
            @Parameter(description = "搜索关键词") @RequestParam(value = "query", defaultValue = "") String query,
            @Parameter(description = "排序方式: hot | latest | relevant", schema = @Schema(allowableValues = {"hot", "latest", "relevant"}))
            @RequestParam(value = "sort", defaultValue = "latest") String sort,
            @Parameter(description = "页码") @RequestParam(value = "page", defaultValue = "1") int page,
            @Parameter(description = "每页大小") @RequestParam(value = "size", defaultValue = "20") int size) {
        return agentFacadeService.searchPosts(query, sort, page, size);
    }

    // ── Get Post ───────────────────────────────────────────────────

    @GetMapping("/posts/{postId}")
    @Operation(summary = "获取帖子详情")
    public AgentPostContext getPost(
            @Parameter(description = "帖子ID") @PathVariable("postId") long postId) {
        return agentFacadeService.getPost(postId);
    }

    @DeleteMapping("/posts/{postId}")
    @Operation(summary = "Delete an owned published post")
    @ApiResponse(responseCode = "204", description = "Post deleted")
    public ResponseEntity<Void> deletePost(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "Post ID") @PathVariable("postId") long postId,
            @Parameter(in = ParameterIn.HEADER, name = "Idempotency-Key")
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        long userId = jwtService.extractUserId(jwt);
        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            idempotencyService.execute(userId, "DELETE_POST", idempotencyKey, "{}", Void.class,
                    () -> { agentFacadeService.deletePost(userId, postId); return null; });
        } else {
            agentFacadeService.deletePost(userId, postId);
        }
        return ResponseEntity.noContent().build();
    }

    // ── My Posts ───────────────────────────────────────────────────

    @GetMapping("/me/posts")
    @Operation(summary = "查询自己的帖子")
    public List<AgentOwnPostSummary> getMyPosts(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "页码") @RequestParam(value = "page", defaultValue = "1") int page,
            @Parameter(description = "每页大小") @RequestParam(value = "size", defaultValue = "20") int size) {
        long userId = jwtService.extractUserId(jwt);
        return agentFacadeService.getMyPosts(userId, page, size);
    }

    // ── Drafts ─────────────────────────────────────────────────────

    @PostMapping("/drafts")
    @Operation(summary = "创建草稿")
    @ApiResponse(responseCode = "201", description = "草稿创建成功")
    public ResponseEntity<DraftResponse> createDraft(
            @AuthenticationPrincipal Jwt jwt,
            @Valid @RequestBody AgentDraftCreateRequest request,
            @Parameter(in = ParameterIn.HEADER, name = "Idempotency-Key", description = "幂等键")
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        long userId = jwtService.extractUserId(jwt);
        DraftResponse result;
        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            String requestBody = toJson(request);
            result = idempotencyService.execute(userId, "CREATE_DRAFT", idempotencyKey, requestBody,
                    DraftResponse.class,
                    () -> agentFacadeService.createDraft(userId, request));
        } else {
            result = agentFacadeService.createDraft(userId, request);
        }
        return ResponseEntity.status(HttpStatus.CREATED).body(result);
    }

    @GetMapping("/drafts/{draftId}")
    @Operation(summary = "获取草稿")
    public DraftResponse getDraft(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "草稿ID") @PathVariable("draftId") long draftId) {
        long userId = jwtService.extractUserId(jwt);
        return agentFacadeService.getDraft(userId, draftId);
    }

    @GetMapping("/me/drafts")
    @Operation(summary = "查询自己的草稿列表")
    public List<DraftResponse> getMyDrafts(
            @AuthenticationPrincipal Jwt jwt) {
        long userId = jwtService.extractUserId(jwt);
        return agentFacadeService.getMyDrafts(userId);
    }

    @PutMapping("/drafts/{draftId}")
    @Operation(summary = "修改草稿")
    public DraftResponse updateDraft(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "草稿ID") @PathVariable("draftId") long draftId,
            @Valid @RequestBody AgentDraftUpdateRequest request,
            @Parameter(in = ParameterIn.HEADER, name = "Idempotency-Key")
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        long userId = jwtService.extractUserId(jwt);
        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            String requestBody = toJson(request);
            return idempotencyService.execute(userId, "UPDATE_DRAFT", idempotencyKey, requestBody,
                    DraftResponse.class,
                    () -> agentFacadeService.updateDraft(userId, draftId, request));
        }
        return agentFacadeService.updateDraft(userId, draftId, request);
    }

    @DeleteMapping("/drafts/{draftId}")
    @Operation(summary = "Delete draft")
    @ApiResponse(responseCode = "204", description = "Draft deleted")
    public ResponseEntity<Void> deleteDraft(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "Draft ID") @PathVariable("draftId") long draftId,
            @Parameter(in = ParameterIn.HEADER, name = "Idempotency-Key")
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        long userId = jwtService.extractUserId(jwt);
        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            idempotencyService.execute(userId, "DELETE_DRAFT", idempotencyKey, "{}", Void.class,
                    () -> { agentFacadeService.deleteDraft(userId, draftId); return null; });
        } else {
            agentFacadeService.deleteDraft(userId, draftId);
        }
        return ResponseEntity.noContent().build();
    }

    // ── Publications ───────────────────────────────────────────────

    @PostMapping("/publications/schedules")
    @Operation(summary = "创建定时发布任务")
    @ApiResponse(responseCode = "201", description = "定时任务创建成功")
    public ResponseEntity<ScheduledPublicationResponse> createSchedule(
            @AuthenticationPrincipal Jwt jwt,
            @Valid @RequestBody ScheduleCreateRequest request,
            @Parameter(in = ParameterIn.HEADER, name = "Idempotency-Key")
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        long userId = jwtService.extractUserId(jwt);
        ScheduledPublicationResponse result;
        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            String requestBody = toJson(request);
            result = idempotencyService.execute(userId, "CREATE_SCHEDULE", idempotencyKey, requestBody,
                    ScheduledPublicationResponse.class,
                    () -> scheduledPublicationService.schedule(userId, request, idempotencyKey));
        } else {
            result = scheduledPublicationService.schedule(userId, request, idempotencyKey);
        }
        return ResponseEntity.status(HttpStatus.CREATED).body(result);
    }

    @GetMapping("/publications/schedules/{scheduleId}")
    @Operation(summary = "获取定时任务")
    public ScheduledPublicationResponse getSchedule(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "定时任务ID") @PathVariable("scheduleId") long scheduleId) {
        long userId = jwtService.extractUserId(jwt);
        return scheduledPublicationService.get(userId, scheduleId);
    }

    @GetMapping("/publications/schedules")
    @Operation(summary = "查询自己的定时发布")
    public List<ScheduledPublicationResponse> getMySchedules(
            @AuthenticationPrincipal Jwt jwt) {
        long userId = jwtService.extractUserId(jwt);
        return scheduledPublicationService.listByUser(userId);
    }

    @PutMapping("/publications/schedules/{scheduleId}")
    @Operation(summary = "修改定时发布时间")
    public ScheduledPublicationResponse updateSchedule(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "定时任务ID") @PathVariable("scheduleId") long scheduleId,
            @Valid @RequestBody ScheduleUpdateRequest request,
            @Parameter(in = ParameterIn.HEADER, name = "Idempotency-Key")
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        long userId = jwtService.extractUserId(jwt);
        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            String requestBody = toJson(request);
            return idempotencyService.execute(userId, "UPDATE_SCHEDULE", idempotencyKey, requestBody,
                    ScheduledPublicationResponse.class,
                    () -> scheduledPublicationService.updateRunAt(userId, scheduleId, request));
        }
        return scheduledPublicationService.updateRunAt(userId, scheduleId, request);
    }

    @DeleteMapping("/publications/schedules/{scheduleId}")
    @Operation(summary = "取消定时任务")
    @ApiResponse(responseCode = "204", description = "取消成功")
    public ResponseEntity<Void> cancelSchedule(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "定时任务ID") @PathVariable("scheduleId") long scheduleId,
            @Parameter(in = ParameterIn.HEADER, name = "Idempotency-Key")
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        long userId = jwtService.extractUserId(jwt);
        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            idempotencyService.execute(userId, "CANCEL_SCHEDULE", idempotencyKey, "{}", Void.class,
                    () -> { scheduledPublicationService.cancel(userId, scheduleId); return null; });
        } else {
            scheduledPublicationService.cancel(userId, scheduleId);
        }
        return ResponseEntity.noContent().build();
    }

    @PostMapping("/publications/publish-now")
    @Operation(summary = "立即发布")
    public PublishResponse publishNow(
            @AuthenticationPrincipal Jwt jwt,
            @Valid @RequestBody PublishNowRequest request,
            @Parameter(in = ParameterIn.HEADER, name = "Idempotency-Key")
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        long userId = jwtService.extractUserId(jwt);
        long draftId;
        try { draftId = Long.parseLong(request.draftId()); }
        catch (NumberFormatException e) { throw new BusinessException(ErrorCode.BAD_REQUEST, "draftId 格式不正确"); }

        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            String requestBody = toJson(request);
            return idempotencyService.execute(userId, "PUBLISH_NOW", idempotencyKey, requestBody,
                    PublishResponse.class,
                    () -> scheduledPublicationService.publishNow(userId, draftId));
        }
        return scheduledPublicationService.publishNow(userId, draftId);
    }

    // ── Comments ───────────────────────────────────────────────────

    @GetMapping("/posts/{postId}/comments")
    @Operation(summary = "查询帖子评论")
    public AgentCommentPageResponse getPostComments(
            @Parameter(description = "帖子ID") @PathVariable("postId") long postId,
            @Parameter(description = "分页游标") @RequestParam(value = "cursor", required = false) String cursor,
            @Parameter(description = "每页大小") @RequestParam(value = "size", defaultValue = "20") int size) {
        return agentFacadeService.getPostComments(postId, cursor, size);
    }

    @GetMapping("/comments/{commentId}")
    @Operation(summary = "查询单条评论")
    public AgentCommentResponse getComment(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "评论ID") @PathVariable("commentId") long commentId) {
        long userId = jwtService.extractUserId(jwt);
        return agentFacadeService.getComment(userId, String.valueOf(commentId));
    }

    @PostMapping("/comments/{commentId}/replies")
    @Operation(summary = "回复评论")
    @ApiResponse(responseCode = "201", description = "回复成功")
    public ResponseEntity<AgentCommentResponse> replyToComment(
            @AuthenticationPrincipal Jwt jwt,
            @Parameter(description = "父评论ID") @PathVariable("commentId") long commentId,
            @Valid @RequestBody AgentCommentReplyRequest request,
            @Parameter(in = ParameterIn.HEADER, name = "Idempotency-Key")
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        long userId = jwtService.extractUserId(jwt);
        AgentCommentResponse result;
        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            String requestBody = toJson(request);
            result = idempotencyService.execute(userId, "REPLY_COMMENT", idempotencyKey, requestBody,
                    AgentCommentResponse.class,
                    () -> agentFacadeService.replyToComment(userId, request.postId(), request.parentCommentId(), request.content()));
        } else {
            result = agentFacadeService.replyToComment(userId, request.postId(), request.parentCommentId(), request.content());
        }
        return ResponseEntity.status(HttpStatus.CREATED).body(result);
    }

    // ── Analytics ──────────────────────────────────────────────────

    @GetMapping("/posts/{postId}/analytics")
    @Operation(summary = "获取帖子分析数据")
    public PostAnalyticsResponse getPostAnalytics(
            @Parameter(description = "帖子ID") @PathVariable("postId") long postId) {
        return agentFacadeService.getPostAnalytics(postId);
    }

    @GetMapping("/me/analytics/summary")
    @Operation(summary = "获取个人分析摘要")
    public UserAnalyticsSummaryResponse getMyAnalyticsSummary(
            @AuthenticationPrincipal Jwt jwt) {
        long userId = jwtService.extractUserId(jwt);
        return agentFacadeService.getMyAnalyticsSummary(userId);
    }

    // ── helpers ────────────────────────────────────────────────────

    private String toJson(Object obj) {
        try { return objectMapper.writeValueAsString(obj); }
        catch (JsonProcessingException e) { return "{}"; }
    }
}
