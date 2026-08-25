package com.tongji.search.projection;

import com.tongji.knowpost.event.PostLifecycleEvent;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.search.ElasticsearchPostClient;
import com.tongji.search.PostSearchDocument;
import com.tongji.search.PostSearchDocumentService;
import com.tongji.search.SearchProjectionMetrics;
import com.tongji.relation.outbox.OutboxTopics;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Service;

@Service
public class ElasticsearchPostProjectionConsumer {
    private final ElasticsearchPostClient elasticsearch;
    private final PostSearchDocumentService documents;
    private final PostSearchProjectionMessageParser parser;
    private final SearchProjectionMetrics metrics;

    public ElasticsearchPostProjectionConsumer(ElasticsearchPostClient elasticsearch,
                                               PostSearchDocumentService documents,
                                               PostSearchProjectionMessageParser parser,
                                               SearchProjectionMetrics metrics) {
        this.elasticsearch = elasticsearch;
        this.documents = documents;
        this.parser = parser;
        this.metrics = metrics;
    }

    @KafkaListener(topics = OutboxTopics.POST_SEARCH_PROJECTION,
            groupId = "search-es-projection",
            containerFactory = "searchProjectionKafkaListenerContainerFactory")
    public void onMessage(String message, Acknowledgment acknowledgment) {
        try {
            for (PostLifecycleEvent event : parser.parse(message)) apply(event);
            acknowledgment.acknowledge();
        } catch (RuntimeException e) {
            metrics.failure();
            throw e;
        }
    }

    public void apply(PostLifecycleEvent event) {
        KnowPost post = documents.find(event.postId());
        if (post != null && post.getEventVersion() != null
                && post.getEventVersion() > event.eventVersion()) {
            metrics.stale();
            return;
        }
        if (!documents.searchable(post)) {
            elasticsearch.delete(event.postId(), event.eventVersion());
            metrics.deleted();
            return;
        }
        PostSearchDocument document = documents.build(post);
        elasticsearch.upsert(document);
        metrics.appliedEs(document.updatedAt());
    }
}
