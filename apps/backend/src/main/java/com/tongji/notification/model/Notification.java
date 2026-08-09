package com.tongji.notification.model;

import lombok.Builder;
import lombok.Data;

import java.time.Instant;

@Data
@Builder
public class Notification {
    private Long id;
    private String eventId;
    private Long receiverId;
    private Long actorId;
    private Long latestActorId;
    private String type;
    private String targetType;
    private String targetId;
    private String aggregateType;
    private String aggregateId;
    private String title;
    private String content;
    private String extraJson;
    private Integer actorCount;
    private Boolean readStatus;
    private Instant createTime;
    private Instant updateTime;
    private Instant readTime;
}
