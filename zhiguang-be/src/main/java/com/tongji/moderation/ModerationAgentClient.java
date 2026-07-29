package com.tongji.moderation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.tongji.common.trace.TraceContextFilter;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Locale;
import java.util.Map;

@Component
public class ModerationAgentClient {

    private static final Logger log = LoggerFactory.getLogger(ModerationAgentClient.class);

    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;
    private final String baseUrl;
    private final String authSecret;
    private final Duration timeout;

    public ModerationAgentClient(
            ObjectMapper objectMapper,
            @Value("${moderation-agent.base-url:http://127.0.0.1:8088}") String baseUrl,
            @Value("${moderation-agent.auth-secret:}") String authSecret,
            @Value("${moderation-agent.timeout-seconds:30}") int timeoutSeconds
    ) {
        this.objectMapper = objectMapper;
        this.baseUrl = baseUrl == null ? "http://127.0.0.1:8088" : baseUrl.replaceAll("/$", "");
        this.authSecret = authSecret == null ? "" : authSecret;
        this.timeout = Duration.ofSeconds(Math.max(5, timeoutSeconds));
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(this.timeout)
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    public String submitTask(
            String content,
            String contentId,
            String creatorId,
            String idempotencyKey,
            String traceId
    ) {
        try {
            ObjectNode body = objectMapper.createObjectNode();
            body.put("content", content == null ? "" : content);
            body.put("content_type", "POST");
            if (contentId != null) {
                body.put("content_id", contentId);
            }
            body.put("platform", "zhiguang");
            if (creatorId != null) {
                body.put("creator_id", creatorId);
            }
            body.set("metadata", objectMapper.valueToTree(Map.of("channel", "knowpost")));
            if (idempotencyKey != null && !idempotencyKey.isBlank()) {
                body.put("idempotency_key", idempotencyKey);
            }
            if (traceId != null && !traceId.isBlank()) {
                body.put("trace_id", traceId);
            }

            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl + "/moderation/tasks"))
                    .timeout(timeout)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(body)));
            if (traceId != null && !traceId.isBlank()) {
                builder.header("X-Trace-ID", traceId);
            }
            if (!authSecret.isBlank()) {
                builder.header("Authorization", "Bearer " + authSecret);
            }

            HttpResponse<String> response = httpClient.send(builder.build(), HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 201 && response.statusCode() != 202) {
                throw new BusinessException(ErrorCode.BAD_REQUEST,
                        "审核服务提交失败：" + response.statusCode());
            }
            JsonNode root = objectMapper.readTree(response.body());
            JsonNode task = root.path("task");
            String taskId = task.path("id").asText(null);
            if (taskId == null || taskId.isBlank()) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "审核服务未返回任务 ID");
            }
            return taskId;
        } catch (BusinessException e) {
            throw e;
        } catch (Exception e) {
            log.warn("Submit moderation task failed: {}", e.getMessage());
            throw new BusinessException(ErrorCode.BAD_REQUEST, "审核服务暂不可用，请稍后重试");
        }
    }

    public ModerationAgentDecision getTask(String taskId) {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl + "/moderation/tasks/" + taskId))
                    .timeout(timeout)
                    .header(TraceContextFilter.HEADER, TraceContextFilter.currentOrCreate())
                    .GET();
            if (!authSecret.isBlank()) {
                builder.header("Authorization", "Bearer " + authSecret);
            }
            HttpResponse<String> response = httpClient.send(builder.build(), HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) {
                throw new BusinessException(ErrorCode.BAD_REQUEST,
                        "审核任务查询失败：" + response.statusCode());
            }
            JsonNode root = objectMapper.readTree(response.body());
            String status = root.path("status").asText("").toUpperCase(Locale.ROOT);
            String finalAction = root.path("final_action").asText("").toUpperCase(Locale.ROOT);
            if (finalAction.isBlank()) {
                finalAction = root.path("agent_decision").path("recommended_action").asText("").toUpperCase(Locale.ROOT);
            }
            String reason = root.path("agent_decision").path("reason").asText("");
            return new ModerationAgentDecision(
                    root.path("id").asText(taskId),
                    status,
                    finalAction,
                    reason
            );
        } catch (BusinessException e) {
            throw e;
        } catch (Exception e) {
            log.warn("Get moderation task failed: {}", e.getMessage());
            throw new BusinessException(ErrorCode.BAD_REQUEST, "审核任务查询失败");
        }
    }

    public JsonNode getStatistics() {
        return sendAdminRequest("GET", "/moderation/statistics", null);
    }

    public JsonNode listTasks(String status, int limit, int offset) {
        int safeLimit = Math.max(1, Math.min(200, limit));
        int safeOffset = Math.max(0, offset);
        StringBuilder path = new StringBuilder("/moderation/tasks?limit=")
                .append(safeLimit)
                .append("&offset=")
                .append(safeOffset);
        if (status != null && !status.isBlank()) {
            path.append("&status=")
                    .append(URLEncoder.encode(status.trim(), StandardCharsets.UTF_8));
        }
        return sendAdminRequest("GET", path.toString(), null);
    }

    public JsonNode listCallbacks(String status, int limit, int offset) {
        int safeLimit = Math.max(1, Math.min(200, limit));
        int safeOffset = Math.max(0, offset);
        StringBuilder path = new StringBuilder("/moderation/callbacks?limit=")
                .append(safeLimit)
                .append("&offset=")
                .append(safeOffset);
        if (status != null && !status.isBlank()) {
            path.append("&status=")
                    .append(URLEncoder.encode(status.trim(), StandardCharsets.UTF_8));
        }
        return sendAdminRequest("GET", path.toString(), null);
    }

    public JsonNode getTaskJson(String taskId) {
        return sendAdminRequest("GET", "/moderation/tasks/" + safeTaskId(taskId), null);
    }

    public JsonNode submitHumanReview(
            String taskId,
            String reviewerId,
            String action,
            String riskType,
            String comment,
            Integer expectedVersion
    ) {
        ObjectNode body = objectMapper.createObjectNode();
        body.put("action", action);
        body.put("reviewer_id", reviewerId);
        if (riskType != null && !riskType.isBlank()) {
            body.put("risk_type", riskType);
        }
        if (comment != null && !comment.isBlank()) {
            body.put("comment", comment);
        }
        if (expectedVersion != null) {
            body.put("expected_version", expectedVersion);
        }
        body.put("idempotency_key", "admin-review:" + taskId + ":" + expectedVersion + ":" + action);
        return sendAdminRequest("POST", "/moderation/tasks/" + safeTaskId(taskId) + "/review", body);
    }

    private String safeTaskId(String taskId) {
        if (taskId == null || !taskId.matches("[0-9a-fA-F-]{36}")) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "审核任务 ID 无效");
        }
        return taskId;
    }

    private JsonNode sendAdminRequest(String method, String path, JsonNode payload) {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl + path))
                    .timeout(timeout)
                    .header("Accept", "application/json")
                    .header(TraceContextFilter.HEADER, TraceContextFilter.currentOrCreate());
            if (!authSecret.isBlank()) {
                builder.header("Authorization", "Bearer " + authSecret);
            }
            if ("POST".equals(method)) {
                builder.header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(
                                payload == null ? "{}" : objectMapper.writeValueAsString(payload)
                        ));
            } else {
                builder.GET();
            }
            HttpResponse<String> response = httpClient.send(
                    builder.build(),
                    HttpResponse.BodyHandlers.ofString()
            );
            JsonNode body = response.body() == null || response.body().isBlank()
                    ? objectMapper.createObjectNode()
                    : objectMapper.readTree(response.body());
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                String detail = body.path("detail").asText("审核服务请求失败");
                throw new BusinessException(ErrorCode.BAD_REQUEST, detail);
            }
            return body;
        } catch (BusinessException e) {
            throw e;
        } catch (Exception e) {
            log.warn("Admin moderation request failed: {}", e.getMessage());
            throw new BusinessException(ErrorCode.BAD_REQUEST, "审核服务暂不可用，请稍后重试");
        }
    }

    public String submitReview(
            String title,
            String description,
            String tags,
            String content,
            String contentId,
            String creatorId,
            String idempotencyKey,
            String traceId
    ) {
        StringBuilder sb = new StringBuilder();
        if (title != null && !title.isBlank()) {
            sb.append("标题：").append(title).append('\n');
        }
        if (description != null && !description.isBlank()) {
            sb.append("摘要：").append(description).append('\n');
        }
        if (tags != null && !tags.isBlank()) {
            sb.append("标签：").append(tags).append('\n');
        }
        sb.append("正文：\n").append(content == null ? "" : content);
        return submitTask(
                sb.toString(),
                contentId,
                creatorId,
                idempotencyKey,
                traceId
        );
    }

    public record ModerationAgentDecision(String taskId, String status, String finalAction, String reason) {
        public ModerationAgentDecision(String status, String finalAction, String reason) {
            this(null, status, finalAction, reason);
        }

        public boolean terminal() {
            String s = status == null ? "" : status.toUpperCase(Locale.ROOT);
            return "COMPLETED".equals(s) || "FAILED".equals(s) || "WAITING_REVIEW".equals(s);
        }

        public boolean pass() {
            return "COMPLETED".equalsIgnoreCase(status)
                    && "PASS".equalsIgnoreCase(finalAction);
        }

        public boolean reject() {
            return "COMPLETED".equalsIgnoreCase(status)
                    && ("REJECT".equalsIgnoreCase(finalAction)
                    || "LIMIT".equalsIgnoreCase(finalAction));
        }

        public static ModerationAgentDecision failed(String reason) {
            return new ModerationAgentDecision(null, "FAILED", "REJECT", reason);
        }

        public static ModerationAgentDecision pending(String reason) {
            return new ModerationAgentDecision(null, "RUNNING", "", reason);
        }
    }
}
