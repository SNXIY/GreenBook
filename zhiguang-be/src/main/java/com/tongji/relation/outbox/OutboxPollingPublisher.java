package com.tongji.relation.outbox;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

@Slf4j
@Component
@RequiredArgsConstructor
public class OutboxPollingPublisher {
    private final OutboxMapper outboxMapper;
    private final KafkaTemplate<String, String> kafka;
    private final ObjectMapper objectMapper;

    @Value("${outbox.polling.enabled:true}")
    private boolean enabled;

    @Value("${outbox.polling.batch-size:100}")
    private int batchSize;

    @Scheduled(fixedDelayString = "${outbox.polling.fixed-delay-ms:3000}",
            initialDelayString = "${outbox.polling.initial-delay-ms:5000}")
    public void publishNewEvents() {
        if (!enabled) {
            return;
        }
        int limit = Math.min(Math.max(batchSize, 1), 500);
        List<Map<String, Object>> rows = outboxMapper.selectNew(limit);
        if (rows == null || rows.isEmpty()) {
            return;
        }

        ArrayNode data = objectMapper.createArrayNode();
        List<Long> ids = new ArrayList<>(rows.size());
        for (Map<String, Object> row : rows) {
            Object id = row.get("id");
            if (!(id instanceof Number number)) {
                continue;
            }
            ids.add(number.longValue());
            ObjectNode node = objectMapper.createObjectNode();
            put(node, "id", row.get("id"));
            put(node, "aggregate_type", row.get("aggregate_type"));
            put(node, "aggregate_id", row.get("aggregate_id"));
            put(node, "type", row.get("type"));
            put(node, "payload", row.get("payload"));
            put(node, "status", row.get("status"));
            data.add(node);
        }
        if (ids.isEmpty()) {
            return;
        }

        ObjectNode message = objectMapper.createObjectNode();
        message.put("table", "outbox");
        message.put("type", "INSERT");
        message.set("data", data);

        try {
            kafka.send(OutboxTopics.CANAL_OUTBOX, objectMapper.writeValueAsString(message))
                    .get(10, TimeUnit.SECONDS);
            outboxMapper.markPublished(ids);
            log.info("Outbox polling published {} events", ids.size());
        } catch (Exception ex) {
            log.warn("Outbox polling publish failed, ids={}, error={}", ids, ex.getMessage());
        }
    }

    private void put(ObjectNode node, String field, Object value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, String.valueOf(value));
        }
    }
}
