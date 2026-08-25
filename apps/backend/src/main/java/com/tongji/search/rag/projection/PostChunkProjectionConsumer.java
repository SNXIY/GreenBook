package com.tongji.search.rag.projection;

import com.tongji.knowpost.event.PostLifecycleEvent;
import com.tongji.relation.outbox.OutboxTopics;
import com.tongji.search.rag.PostChunkProjectionService;
import com.tongji.search.projection.PostSearchProjectionMessageParser;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Service;

/** Dedicated consumer group so chunk projection does not alter posts_dense. */
@Service
public class PostChunkProjectionConsumer {
    private final PostChunkProjectionService projection;
    private final PostSearchProjectionMessageParser parser;

    public PostChunkProjectionConsumer(PostChunkProjectionService projection,
                                       PostSearchProjectionMessageParser parser) {
        this.projection = projection;
        this.parser = parser;
    }

    @KafkaListener(topics = OutboxTopics.POST_SEARCH_PROJECTION,
            groupId = "search-qdrant-chunk-projection",
            containerFactory = "searchProjectionKafkaListenerContainerFactory")
    public void onMessage(String message, Acknowledgment acknowledgment) {
        try {
            for (PostLifecycleEvent event : parser.parse(message)) projection.apply(event);
            acknowledgment.acknowledge();
        } catch (RuntimeException e) {
            throw e;
        }
    }

    public int apply(PostLifecycleEvent event) {
        return projection.apply(event);
    }
}
