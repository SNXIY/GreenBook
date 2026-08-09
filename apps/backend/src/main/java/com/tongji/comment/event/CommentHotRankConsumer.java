package com.tongji.comment.event;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.comment.service.impl.CommentServiceImpl;
import com.tongji.counter.event.CounterEvent;
import com.tongji.counter.event.CounterTopics;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

@Slf4j
@Component
@RequiredArgsConstructor
public class CommentHotRankConsumer {
    private final ObjectMapper objectMapper;
    private final CommentServiceImpl commentService;

    @KafkaListener(topics = CounterTopics.EVENTS, groupId = "comment-hot-rank")
    public void onCounterEvent(String message, Acknowledgment ack) {
        try {
            CounterEvent event = objectMapper.readValue(message, CounterEvent.class);
            if ("comment".equals(event.getEntityType()) && "like".equals(event.getMetric())) {
                commentService.incrementHotLikeScore(Long.parseLong(event.getEntityId()), event.getDelta());
            }
            ack.acknowledge();
        } catch (Exception ex) {
            log.warn("Refresh comment hot rank failed: {}", ex.getMessage());
        } finally {
            ack.acknowledge();
        }
    }
}
