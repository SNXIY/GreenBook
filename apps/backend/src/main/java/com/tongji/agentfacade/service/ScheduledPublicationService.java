package com.tongji.agentfacade.service;

import com.tongji.agentfacade.api.dto.PublishResponse;
import com.tongji.agentfacade.api.dto.ScheduleCreateRequest;
import com.tongji.agentfacade.api.dto.ScheduleUpdateRequest;
import com.tongji.agentfacade.api.dto.ScheduledPublicationResponse;
import com.tongji.agentfacade.mapper.ScheduledPublicationMapper;
import com.tongji.agentfacade.mapper.ScheduledPublicationRecord;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.knowpost.id.SnowflakeIdGenerator;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.notification.service.NotificationService;
import com.tongji.relation.outbox.OutboxMapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;
import java.util.Map;

@Service
@RequiredArgsConstructor
public class ScheduledPublicationService {

    private static final Logger log = LoggerFactory.getLogger(ScheduledPublicationService.class);

    private final ScheduledPublicationMapper mapper;
    private final KnowPostMapper knowPostMapper;
    private final KnowPostService knowPostService;
    private final SnowflakeIdGenerator idGen;
    private final OutboxMapper outboxMapper;
    private final ObjectMapper objectMapper;
    private final NotificationService notificationService;

    @Transactional
    public ScheduledPublicationResponse schedule(long userId, ScheduleCreateRequest request, String idempotencyKey) {
        long draftId = parseLong(request.draftId(), "draftId");

        // Shares the post-row lock with AgentFacadeService.deleteDraft(), so
        // a delete cannot race a just-created future schedule into a silent
        // scheduler failure.
        KnowPost draft = knowPostMapper.findByIdForUpdate(draftId);
        if (draft == null || draft.getCreatorId() == null || !draft.getCreatorId().equals(userId)) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或不属于当前用户");
        }
        if (!"draft".equals(draft.getStatus())) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "只能对草稿状态的内容创建定时发布");
        }

        String tz = request.timezone() != null ? request.timezone() : "Asia/Shanghai";

        // Check for existing schedule with same idempotency key
        if (idempotencyKey != null && !idempotencyKey.isBlank()) {
            ScheduledPublicationRecord existing = mapper.findByUserAndIdempotencyKey(userId, idempotencyKey);
            if (existing != null) {
                return toResponse(existing);
            }
        }

        long id = idGen.nextId();
        mapper.insert(id, userId, draftId, request.runAt(), tz, "SCHEDULED", idempotencyKey, "user:" + userId);
        writeOutboxEvent("publication.scheduled", draftId, userId, Map.of("scheduleId", String.valueOf(id)));

        ScheduledPublicationRecord record = mapper.findById(id);
        return toResponse(record);
    }

    @Transactional(readOnly = true)
    public ScheduledPublicationResponse get(long userId, long scheduleId) {
        ScheduledPublicationRecord record = mapper.findById(scheduleId);
        if (record == null || record.getUserId() == null || record.getUserId() != userId) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "定时任务不存在");
        }
        return toResponse(record);
    }

    @Transactional(readOnly = true)
    public List<ScheduledPublicationResponse> listByUser(long userId) {
        return mapper.findByUser(userId).stream()
                .map(this::toResponse)
                .toList();
    }

    @Transactional
    public ScheduledPublicationResponse updateRunAt(long userId, long scheduleId, ScheduleUpdateRequest request) {
        int updated = mapper.updateRunAt(scheduleId, userId, request.runAt(), request.version());
        if (updated == 0) {
            ScheduledPublicationRecord record = mapper.findById(scheduleId);
            if (record == null || record.getUserId() == null || record.getUserId() != userId) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "定时任务不存在");
            }
            throw new BusinessException(ErrorCode.BAD_REQUEST, "定时任务已被修改，请刷新后重试");
        }
        ScheduledPublicationRecord record = mapper.findById(scheduleId);
        return toResponse(record);
    }

    @Transactional
    public void cancel(long userId, long scheduleId) {
        int updated = mapper.cancel(scheduleId, userId, "CANCELLED");
        if (updated == 0) {
            ScheduledPublicationRecord record = mapper.findById(scheduleId);
            if (record == null || record.getUserId() == null || record.getUserId() != userId) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "定时任务不存在");
            }
            // Idempotent: already cancelled
        }
        if (updated > 0) {
            writeOutboxEvent("publication.cancelled", scheduleId, userId, Map.of("scheduleId", String.valueOf(scheduleId)));
        }
    }

    @Transactional
    public PublishResponse publishNow(long userId, long draftId) {
        KnowPost draft = knowPostMapper.findById(draftId);
        if (draft == null || draft.getCreatorId() == null || !draft.getCreatorId().equals(userId)) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿不存在或不属于当前用户");
        }

        boolean alreadyPublished = "published".equals(draft.getStatus());
        String status;
        if (!alreadyPublished) {
            if (!"draft".equals(draft.getStatus())) {
                throw new BusinessException(ErrorCode.BAD_REQUEST, "草稿当前状态不可发布");
            }
            status = knowPostService.publish(userId, draftId);
        } else {
            status = "published";
        }

        if (!alreadyPublished && "published".equals(status)) {
            writeOutboxEvent("publication.published", draftId, userId, Map.of("postId", String.valueOf(draftId)));
            notifyPublishedSafely(userId, draftId, null);
        }
        // An immediate publish supersedes any future trigger for the same
        // draft. Keep this in the Java transaction so the scheduler cannot
        // observe a published draft with a still-active SCHEDULED row.
        neutralizeFutureSchedules(userId, draftId);

        return new PublishResponse(
                String.valueOf(draftId), status, alreadyPublished,
                draft.getPublishTime() != null ? draft.getPublishTime() : Instant.now());
    }

    private void neutralizeFutureSchedules(long userId, long draftId) {
        for (ScheduledPublicationRecord record : mapper.findByUser(userId)) {
            if (record.getDraftId() == null || record.getDraftId() != draftId
                    || !"SCHEDULED".equals(record.getStatus())) {
                continue;
            }
            if (mapper.cancel(record.getId(), userId, "CANCELLED") > 0) {
                writeOutboxEvent("publication.cancelled", record.getId(), userId,
                        Map.of("scheduleId", String.valueOf(record.getId()),
                                "reason", "PUBLISH_NOW"));
            }
        }
    }

    @Scheduled(fixedDelayString = "${agent.publication.scheduler-delay-ms:30000}",
            initialDelayString = "${agent.publication.scheduler-initial-delay-ms:30000}")
    public void executeDuePublications() {
        List<ScheduledPublicationRecord> due = mapper.findDue(Instant.now(), 10);
        for (ScheduledPublicationRecord record : due) {
            // Atomically claim the record: only one worker succeeds
            int claimed = mapper.claimForExecution(record.getId());
            if (claimed == 0) {
                log.debug("Schedule {} already claimed by another worker", record.getId());
                continue;
            }
            log.info("Schedule {} claimed for execution", record.getId());
            try {
                executePublication(record);
            } catch (Exception e) {
                log.warn("Scheduled publication threw exception: scheduleId={}, draftId={}, error={}",
                        record.getId(), record.getDraftId(), e.getMessage());
                int mf = mapper.markFailed(record.getId(), "PUBLICATION_FAILED",
                        truncate(e.getMessage(), 500));
                if (mf != 1) {
                    log.error("CONSISTENCY: exception-handler markFailed affected {} rows for scheduleId={}, expected 1",
                            mf, record.getId());
                }
                writeOutboxEvent("publication.failed", record.getDraftId(), record.getUserId(),
                        Map.of("scheduleId", String.valueOf(record.getId()),
                                "error", truncate(e.getMessage(), 200)));
            }
        }
    }

    @Scheduled(fixedDelayString = "${agent.publication.recovery-delay-ms:120000}",
            initialDelayString = "${agent.publication.recovery-initial-delay-ms:120000}")
    public void recoverStaleProcessing() {
        Instant staleThreshold = Instant.now().minusSeconds(120);
        List<ScheduledPublicationRecord> stale = mapper.recoverStaleProcessing(staleThreshold, 5);
        for (ScheduledPublicationRecord record : stale) {
            log.warn("Recovering stale PROCESSING schedule: id={}, draftId={}", record.getId(), record.getDraftId());
            int mf = mapper.markFailed(record.getId(), "WORKER_TIMEOUT", "执行超时，需要人工重试");
            if (mf != 1) {
                log.error("CONSISTENCY: recovery markFailed affected {} rows for scheduleId={}, expected 1",
                        mf, record.getId());
            }
            writeOutboxEvent("publication.failed", record.getDraftId(), record.getUserId(),
                    Map.of("scheduleId", String.valueOf(record.getId()), "reason", "worker_timeout"));
        }
    }

    private void executePublication(ScheduledPublicationRecord record) {
        KnowPost draft = knowPostMapper.findById(record.getDraftId());
        if (draft == null) {
            int mf = mapper.markFailed(record.getId(), "DRAFT_NOT_FOUND", "草稿已被删除");
            if (mf != 1) {
                log.error("CONSISTENCY: markFailed affected {} rows for scheduleId={} (DRAFT_NOT_FOUND), expected 1",
                        mf, record.getId());
            }
            writeOutboxEvent("publication.failed", record.getDraftId(), record.getUserId(),
                    Map.of("scheduleId", String.valueOf(record.getId()), "reason", "draft_not_found"));
            return;
        }
        if (!"draft".equals(draft.getStatus())) {
            int mf = mapper.markFailed(record.getId(), "DRAFT_STATUS_INVALID",
                    "草稿状态为 " + draft.getStatus() + "，不可发布");
            if (mf != 1) {
                log.error("CONSISTENCY: markFailed affected {} rows for scheduleId={} (DRAFT_STATUS_INVALID), expected 1",
                        mf, record.getId());
            }
            writeOutboxEvent("publication.failed", record.getDraftId(), record.getUserId(),
                    Map.of("scheduleId", String.valueOf(record.getId()),
                            "reason", "draft_status_" + draft.getStatus()));
            return;
        }

        String status = knowPostService.publish(record.getUserId(), record.getDraftId());
        if ("published".equals(status)) {
            int mp = mapper.markPublished(record.getId(), record.getDraftId(), Instant.now());
            if (mp != 1) {
                log.error("CONSISTENCY: markPublished affected {} rows for scheduleId={}, post already published but " +
                        "scheduled_publications state update failed — manual reconciliation required", mp, record.getId());
            }
            writeOutboxEvent("publication.published", record.getDraftId(), record.getUserId(),
                    Map.of("scheduleId", String.valueOf(record.getId()),
                            "postId", String.valueOf(record.getDraftId())));
            notifyPublishedSafely(record);
            log.info("Scheduled publication executed: scheduleId={}, postId={}, markPublishedRows={}",
                    record.getId(), record.getDraftId(), mp);
        } else {
            int mf = mapper.markFailed(record.getId(), "PUBLICATION_STATUS_" + status.toUpperCase(),
                    "发布返回状态: " + status);
            if (mf != 1) {
                log.error("CONSISTENCY: markFailed affected {} rows for scheduleId={} (publish status={}), expected 1",
                        mf, record.getId(), status);
            }
            writeOutboxEvent("publication.failed", record.getDraftId(), record.getUserId(),
                    Map.of("scheduleId", String.valueOf(record.getId()), "status", status));
        }
    }

    private ScheduledPublicationResponse toResponse(ScheduledPublicationRecord r) {
        return new ScheduledPublicationResponse(
                String.valueOf(r.getId()),
                String.valueOf(r.getDraftId()),
                r.getRunAt(),
                r.getTimezone(),
                r.getStatus(),
                r.getVersion(),
                r.getPublishedPostId() != null ? String.valueOf(r.getPublishedPostId()) : null,
                r.getFailureCode(),
                r.getFailureMessage(),
                r.getCreatedAt(),
                r.getUpdatedAt()
        );
    }

    private long parseLong(String value, String label) {
        try {
            return Long.parseLong(value);
        } catch (NumberFormatException e) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, label + " 格式不正确");
        }
    }

    private String truncate(String s, int maxLen) {
        if (s == null) return null;
        return s.length() <= maxLen ? s : s.substring(0, maxLen);
    }

    private void writeOutboxEvent(String type, long aggregateId, long userId, Map<String, String> extra) {
        try {
            long eventId = idGen.nextId();
            Map<String, String> payload = new java.util.LinkedHashMap<>(extra);
            payload.put("userId", String.valueOf(userId));
            outboxMapper.insert(eventId, "publication", aggregateId, type,
                    objectMapper.writeValueAsString(payload));
        } catch (Exception ignored) {
            log.warn("Failed to write outbox event: type={}, aggregateId={}", type, aggregateId);
        }
    }

    /**
     * 发布成功通知是附属能力：失败只能记日志，绝不能把已成功发布的
     * schedule 状态机回退成 FAILED（executeDuePublications 的异常捕获
     * 会 markFailed）。
     */
    private void notifyPublishedSafely(ScheduledPublicationRecord record) {
        try {
            notificationService.notifyPostPublished(
                    "publication.published:" + record.getId(),
                    record.getUserId(), record.getDraftId(), record.getId());
        } catch (Exception ex) {
            log.warn("Failed to notify scheduled publication: scheduleId={}, error={}",
                    record.getId(), ex.getMessage());
        }
    }

    private void notifyPublishedSafely(long userId, long draftId, Long scheduleId) {
        try {
            notificationService.notifyPostPublished(
                    "publication.published:now:" + draftId, userId, draftId, scheduleId);
        } catch (Exception ex) {
            log.warn("Failed to notify immediate publication: userId={}, draftId={}, error={}",
                    userId, draftId, ex.getMessage());
        }
    }
}
