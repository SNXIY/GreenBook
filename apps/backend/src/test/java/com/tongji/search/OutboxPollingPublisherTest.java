package com.tongji.search;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.relation.outbox.OutboxPollingPublisher;
import com.tongji.relation.outbox.OutboxTopics;
import com.tongji.relation.outbox.OutboxMapper;
import org.junit.jupiter.api.Test;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class OutboxPollingPublisherTest {
    @Test
    void kafkaFailureLeavesOutboxRetryableAndObservable() {
        OutboxMapper mapper = mock(OutboxMapper.class);
        KafkaTemplate<String, String> kafka = mock(KafkaTemplate.class);
        when(mapper.selectNew(100)).thenReturn(List.of(Map.of(
                "id", 101L,
                "aggregate_type", "post",
                "aggregate_id", 7L,
                "type", "PostPublished",
                "payload", "{}",
                "status", "NEW")));
        CompletableFuture<SendResult<String, String>> failed = CompletableFuture.failedFuture(
                new IllegalStateException("broker unavailable"));
        when(kafka.send(eq(OutboxTopics.POST_SEARCH_PROJECTION), anyString())).thenReturn(failed);

        OutboxPollingPublisher publisher = new OutboxPollingPublisher(mapper, kafka, new ObjectMapper());
        ReflectionTestUtils.setField(publisher, "enabled", true);
        ReflectionTestUtils.setField(publisher, "batchSize", 100);
        publisher.publishNewEvents();

        verify(mapper).recordPublishFailure(eq(List.of(101L)), contains("broker unavailable"));
        verify(mapper, never()).markPublished(anyList());
    }
}
