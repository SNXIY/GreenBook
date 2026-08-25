package com.tongji.search.projection;

import com.tongji.knowpost.event.PostLifecycleEvent;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.relation.outbox.OutboxTopics;
import com.tongji.search.EmbeddingService;
import com.tongji.search.PostSearchDocument;
import com.tongji.search.PostSearchDocumentService;
import com.tongji.search.QdrantPostClient;
import com.tongji.search.SearchProjectionMetrics;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Service;

@Service
public class QdrantPostProjectionConsumer {
    private final QdrantPostClient qdrant;
    private final EmbeddingService embedding;
    private final PostSearchDocumentService documents;
    private final PostSearchProjectionMessageParser parser;
    private final SearchProjectionMetrics metrics;

    public QdrantPostProjectionConsumer(QdrantPostClient qdrant,
                                        EmbeddingService embedding,
                                        PostSearchDocumentService documents,
                                        PostSearchProjectionMessageParser parser,
                                        SearchProjectionMetrics metrics) {
        this.qdrant = qdrant;
        this.embedding = embedding;
        this.documents = documents;
        this.parser = parser;
        this.metrics = metrics;
    }

    @KafkaListener(topics = OutboxTopics.POST_SEARCH_PROJECTION,
            groupId = "search-qdrant-projection",
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
            qdrant.delete(event.postId(), event.eventVersion());
            metrics.deleted();
            return;
        }
        PostSearchDocument document = documents.build(post);
        qdrant.upsert(document, embedding.embedDocument(document.textForEmbedding()), embedding);
        metrics.appliedQdrant(document.updatedAt());
    }
}
