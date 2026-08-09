package com.tongji.notification.event;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.common.util.OutboxMessageUtil;
import com.tongji.notification.service.NotificationService;
import com.tongji.relation.event.RelationEvent;
import com.tongji.relation.outbox.OutboxTopics;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Service;

import java.util.List;

@Slf4j
@Service
@RequiredArgsConstructor
public class NotificationOutboxConsumer {
    private final ObjectMapper objectMapper;
    private final NotificationService notificationService;

    @KafkaListener(topics = OutboxTopics.CANAL_OUTBOX, groupId = "notification-outbox-consumer")
    public void onMessage(String message, Acknowledgment ack) {
        try {
            List<JsonNode> rows = OutboxMessageUtil.extractRows(objectMapper, message);
            for (JsonNode row : rows) {
                String aggregateType = text(row, "aggregate_type");
                String type = text(row, "type");
                String eventId = text(row, "id");
                String payload = text(row, "payload");
                if (eventId == null || payload == null) {
                    continue;
                }
                if ("following".equals(aggregateType) && "FollowCreated".equals(type)) {
                    RelationEvent event = objectMapper.readValue(payload, RelationEvent.class);
                    notificationService.notifyFollowCreated(eventId, event.fromUserId(), event.toUserId());
                } else if ("comment".equals(aggregateType) && "COMMENT_CREATED".equals(type)) {
                    JsonNode data = objectMapper.readTree(payload);
                    long actorId = data.path("userId").asLong();
                    long postId = data.path("postId").asLong();
                    long postCreatorId = data.path("postCreatorId").asLong();
                    Long parentId = parseNullableLong(data.path("parentId").asText(null));
                    notificationService.notifyCommentCreated(eventId, actorId, postId, parentId, postCreatorId);
                }
            }
            ack.acknowledge();
        } catch (Exception ex) {
            log.warn("Consume outbox notification failed: {}", ex.getMessage());
        }
    }

    private String text(JsonNode row, String field) {
        JsonNode node = row.get(field);
        return node == null || node.isNull() ? null : node.asText();
    }

    private Long parseNullableLong(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        try {
            return Long.parseLong(value);
        } catch (NumberFormatException ex) {
            return null;
        }
    }
}
