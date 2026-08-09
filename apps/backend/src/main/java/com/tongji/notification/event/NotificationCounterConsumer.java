package com.tongji.notification.event;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.counter.event.CounterEvent;
import com.tongji.counter.event.CounterTopics;
import com.tongji.notification.service.NotificationService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Service;

@Slf4j
@Service
@RequiredArgsConstructor
public class NotificationCounterConsumer {
    private final ObjectMapper objectMapper;
    private final NotificationService notificationService;

    @KafkaListener(topics = CounterTopics.EVENTS, groupId = "notification-counter-consumer")
    public void onMessage(String message, Acknowledgment ack) {
        try {
            CounterEvent event = objectMapper.readValue(message, CounterEvent.class);
            if (event.getDelta() > 0 && ("like".equals(event.getMetric()) || "fav".equals(event.getMetric()))) {
                String eventId = event.getEventId() == null || event.getEventId().isBlank()
                        ? "counter:" + event.getMetric() + ":" + event.getEntityType() + ":" + event.getEntityId() + ":" + event.getUserId()
                        : event.getEventId();
                notificationService.notifyReaction(
                        eventId,
                        event.getUserId(),
                        event.getEntityType(),
                        event.getEntityId(),
                        event.getMetric()
                );
            }
            ack.acknowledge();
        } catch (Exception ex) {
            log.warn("Consume counter notification failed: {}", ex.getMessage());
        }
    }
}
