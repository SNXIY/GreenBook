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
public class AgentIdempotencyRecord {
    private Long id;
    private Long userId;
    private String operation;
    private String idempotencyKey;
    private String requestHash;
    private String status;
    private Integer responseStatus;
    private String responseBody;
    private String resourceType;
    private String resourceId;
    private Instant createdAt;
    private Instant completedAt;
    private Instant expiresAt;
}
