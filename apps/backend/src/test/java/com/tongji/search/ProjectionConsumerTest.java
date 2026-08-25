package com.tongji.search;

import com.tongji.knowpost.event.PostLifecycleEvent;
import com.tongji.knowpost.event.PostLifecycleEventType;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.search.projection.ElasticsearchPostProjectionConsumer;
import com.tongji.search.projection.QdrantPostProjectionConsumer;
import com.tongji.search.projection.PostSearchProjectionMessageParser;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

class ProjectionConsumerTest {
    @Test
    void oldEventCannotOverwriteNewerMysqlTruth() {
        ElasticsearchPostClient client = mock(ElasticsearchPostClient.class);
        PostSearchDocumentService docs = mock(PostSearchDocumentService.class);
        PostSearchProjectionMessageParser parser = mock(PostSearchProjectionMessageParser.class);
        SearchProjectionMetrics metrics = new SearchProjectionMetrics();
        KnowPost post = KnowPost.builder().id(7L).eventVersion(5L).status("published").visible("public").build();
        when(docs.find(7L)).thenReturn(post);
        ElasticsearchPostProjectionConsumer consumer = new ElasticsearchPostProjectionConsumer(
                client, docs, parser, metrics);

        consumer.apply(event(7L, 4L, PostLifecycleEventType.PostUpdated));

        verifyNoInteractions(client);
        assertEquals(1L, metrics.snapshot().staleEvents());
    }

    @Test
    void privateVisibilityRemovesBothProjectionKinds() {
        ElasticsearchPostClient es = mock(ElasticsearchPostClient.class);
        QdrantPostClient qdrant = mock(QdrantPostClient.class);
        PostSearchDocumentService docs = mock(PostSearchDocumentService.class);
        when(docs.find(8L)).thenReturn(KnowPost.builder().id(8L).eventVersion(2L)
                .status("published").visible("private").build());
        when(docs.searchable(any())).thenReturn(false);
        SearchProjectionMetrics metrics = new SearchProjectionMetrics();
        ElasticsearchPostProjectionConsumer esConsumer = new ElasticsearchPostProjectionConsumer(
                es, docs, mock(PostSearchProjectionMessageParser.class), metrics);
        QdrantPostProjectionConsumer qdrantConsumer = new QdrantPostProjectionConsumer(
                qdrant, mock(EmbeddingService.class), docs,
                mock(PostSearchProjectionMessageParser.class), metrics);

        esConsumer.apply(event(8L, 2L, PostLifecycleEventType.PostVisibilityChanged));
        qdrantConsumer.apply(event(8L, 2L, PostLifecycleEventType.PostVisibilityChanged));

        verify(es).delete(8L, 2L);
        verify(qdrant).delete(8L, 2L);
        assertEquals(2L, metrics.snapshot().deleteApplied());
    }

    @Test
    void wrapperParserReadsTypedLifecyclePayload() throws Exception {
        PostSearchProjectionMessageParser parser = new PostSearchProjectionMessageParser(new ObjectMapper());
        String payload = "{\"event_id\":1,\"post_id\":9,\"event_version\":3,"
                + "\"event_type\":\"PostPublished\",\"status\":\"published\","
                + "\"visibility\":\"public\",\"user_id\":4,\"tenant_id\":\"zhiguang\"}";
        String wrapper = "{\"table\":\"outbox\",\"data\":[{\"payload\":"
                + new ObjectMapper().writeValueAsString(payload) + "}]}";

        List<PostLifecycleEvent> events = parser.parse(wrapper);

        assertEquals(1, events.size());
        assertEquals(9L, events.get(0).postId());
        assertEquals(PostLifecycleEventType.PostPublished, events.get(0).eventType());
    }

    private PostLifecycleEvent event(long postId, long version, PostLifecycleEventType type) {
        return new PostLifecycleEvent(1L, postId, version, type, "published", "public",
                null, null, null, Instant.now(), 4L, "zhiguang");
    }
}
