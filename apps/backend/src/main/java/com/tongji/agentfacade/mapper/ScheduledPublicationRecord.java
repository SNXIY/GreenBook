package com.tongji.agentfacade.mapper;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.Instant;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class ScheduledPublicationRecord {
    private Long id;
    private Long userId;
    private Long draftId;
    private Instant runAt;
    private String timezone;
    private String status;
    private Integer version;
    private String idempotencyKey;
    private Long publishedPostId;
    private String failureCode;
    private String failureMessage;
    private String auditActor;
    private Instant createdAt;
    private Instant updatedAt;
    private Instant cancelledAt;
    private Instant publishedAt;
}
