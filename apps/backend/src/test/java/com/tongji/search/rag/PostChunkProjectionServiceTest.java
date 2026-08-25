package com.tongji.search.rag;

import com.tongji.knowpost.event.PostLifecycleEvent;
import com.tongji.knowpost.event.PostLifecycleEventType;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.search.EmbeddingService;
import com.tongji.search.PostSearchDocument;
import com.tongji.search.PostSearchDocumentService;
import com.tongji.search.SearchProviderUnavailableException;
import com.tongji.search.rag.config.RagProperties;
import com.tongji.search.rag.mapper.PostChunkMapper;
import com.tongji.search.rag.model.PostChunk;
import com.tongji.search.rag.projection.PostChunkRebuildService;
import com.tongji.search.rag.service.PostChunker;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

class PostChunkProjectionServiceTest {
    @Test
    void staleEventCannotDeleteOrRebuildNewerChunks() {
        PostChunkMapper mapper = mock(PostChunkMapper.class);
        PostSearchDocumentService docs = mock(PostSearchDocumentService.class);
        KnowPostMapper posts = mock(KnowPostMapper.class);
        PostChunker chunker = new PostChunker(128, 16);
        QdrantChunkClient qdrant = mock(QdrantChunkClient.class);
        EmbeddingService embedding = embedding();
        RagProperties properties = properties();
        KnowPost post = KnowPost.builder().id(9L).eventVersion(4L)
                .status("published").visible("public").build();
        when(docs.find(9L)).thenReturn(post);

        PostChunkProjectionService service = new PostChunkProjectionService(
                mapper, docs, posts, chunker, qdrant, embedding, properties);
        service.apply(event(9L, 3L, PostLifecycleEventType.PostUpdated));

        verifyNoInteractions(qdrant, mapper);
        assertEquals(1L, service.metrics().staleCount());
    }

    @Test
    void duplicateEventReplaysStableChunkIdsWithoutDuplicatePoints() {
        PostChunkMapper mapper = mock(PostChunkMapper.class);
        PostSearchDocumentService docs = mock(PostSearchDocumentService.class);
        KnowPostMapper posts = mock(KnowPostMapper.class);
        QdrantChunkClient qdrant = mock(QdrantChunkClient.class);
        EmbeddingService embedding = embedding();
        RagProperties properties = properties();
        KnowPost post = KnowPost.builder().id(10L).eventVersion(2L)
                .status("published").visible("public").title("Java")
                .description("retrieval").tags("[\"java\"]").build();
        when(docs.find(10L)).thenReturn(post);
        when(docs.searchable(post)).thenReturn(true);
        when(docs.build(post)).thenReturn(new PostSearchDocument(
                10L, 1L, "Java", "retrieval", "[\"java\"]",
                "第一段 Java backend\n\nSecond paragraph", "published", "public",
                Instant.now(), Instant.now(), 2L));
        when(embedding.embedDocument(any())).thenReturn(new float[]{1.0f, 0.0f, 0.0f});

        PostChunkProjectionService service = new PostChunkProjectionService(
                mapper, docs, posts, new PostChunker(128, 16), qdrant, embedding, properties);
        PostLifecycleEvent event = event(10L, 2L, PostLifecycleEventType.PostPublished);

        service.apply(event);
        service.apply(event);

        var captor = org.mockito.ArgumentCaptor.forClass(List.class);
        verify(mapper, times(2)).insertBatch(captor.capture());
        List<?> first = captor.getAllValues().get(0);
        List<?> second = captor.getAllValues().get(1);
        assertEquals(first.size(), second.size());
        for (int index = 0; index < first.size(); index++) {
            assertEquals(((PostChunk) first.get(index)).getChunkId(),
                    ((PostChunk) second.get(index)).getChunkId());
        }
        assertEquals(2L, service.metrics().appliedCount());
    }

    @Test
    void privatePostDeletesChunkProjectionOnly() {
        PostChunkMapper mapper = mock(PostChunkMapper.class);
        PostSearchDocumentService docs = mock(PostSearchDocumentService.class);
        KnowPostMapper posts = mock(KnowPostMapper.class);
        QdrantChunkClient qdrant = mock(QdrantChunkClient.class);
        KnowPost post = KnowPost.builder().id(11L).eventVersion(5L)
                .status("published").visible("private").build();
        when(docs.find(11L)).thenReturn(post);
        when(docs.searchable(post)).thenReturn(false);
        PostChunkProjectionService service = new PostChunkProjectionService(
                mapper, docs, posts, new PostChunker(128, 16), qdrant,
                embedding(), properties());

        service.apply(event(11L, 5L, PostLifecycleEventType.PostVisibilityChanged));

        verify(qdrant).deleteByPostId(11L);
        verify(mapper).deleteByPostId(11L);
        assertEquals(1L, service.metrics().deletedCount());
    }

    @Test
    void qdrantUnavailableFailsBeforeMySqlChunkMutation() {
        PostChunkMapper mapper = mock(PostChunkMapper.class);
        PostSearchDocumentService docs = mock(PostSearchDocumentService.class);
        KnowPostMapper posts = mock(KnowPostMapper.class);
        QdrantChunkClient qdrant = mock(QdrantChunkClient.class);
        EmbeddingService embedding = embedding();
        RagProperties properties = properties();
        KnowPost post = KnowPost.builder().id(12L).eventVersion(1L)
                .status("published").visible("public").build();
        when(docs.find(12L)).thenReturn(post);
        when(docs.searchable(post)).thenReturn(true);
        when(docs.build(post)).thenReturn(new PostSearchDocument(
                12L, 1L, "Java", "", "", "paragraph", "published", "public",
                Instant.now(), Instant.now(), 1L));
        doThrow(new SearchProviderUnavailableException("qdrant down"))
                .when(qdrant).deleteByPostId(12L);

        PostChunkProjectionService service = new PostChunkProjectionService(
                mapper, docs, posts, new PostChunker(128, 16), qdrant, embedding, properties);

        assertThrows(SearchProviderUnavailableException.class,
                () -> service.apply(event(12L, 1L, PostLifecycleEventType.PostPublished)));
        verify(mapper, never()).deleteByPostId(12L);
        verify(mapper, never()).insertBatch(any());
        assertEquals(1L, service.metrics().failureCount());
    }

    @Test
    void embeddingUnavailableIsRetryableAndDoesNotChangePostTruth() {
        PostChunkMapper mapper = mock(PostChunkMapper.class);
        PostSearchDocumentService docs = mock(PostSearchDocumentService.class);
        KnowPostMapper posts = mock(KnowPostMapper.class);
        QdrantChunkClient qdrant = mock(QdrantChunkClient.class);
        EmbeddingService embedding = embedding();
        RagProperties properties = properties();
        KnowPost post = KnowPost.builder().id(13L).eventVersion(1L)
                .status("published").visible("public").title("Java").build();
        when(docs.find(13L)).thenReturn(post);
        when(docs.searchable(post)).thenReturn(true);
        when(docs.build(post)).thenReturn(new PostSearchDocument(
                13L, 1L, "Java", "", "", "paragraph", "published", "public",
                Instant.now(), Instant.now(), 1L));
        when(embedding.embedDocument(any()))
                .thenThrow(new SearchProviderUnavailableException("embedding down"));

        PostChunkProjectionService service = new PostChunkProjectionService(
                mapper, docs, posts, new PostChunker(128, 16), qdrant, embedding, properties);

        assertThrows(SearchProviderUnavailableException.class,
                () -> service.apply(event(13L, 1L, PostLifecycleEventType.PostContentUpdated)));
        assertEquals(1L, service.metrics().failureCount());
    }

    @Test
    void rebuildUsesPublicCanonicalPostsAndDedicatedProjection() {
        KnowPostMapper posts = mock(KnowPostMapper.class);
        PostChunkProjectionService projection = mock(PostChunkProjectionService.class);
        KnowPost first = KnowPost.builder().id(21L).eventVersion(2L)
                .status("published").visible("public").creatorId(1L).build();
        KnowPost second = KnowPost.builder().id(22L).eventVersion(3L)
                .status("published").visible("public").creatorId(1L).build();
        when(posts.listPublicForSearchRebuild(2, 0)).thenReturn(List.of(first, second));
        when(posts.listPublicForSearchRebuild(2, 2)).thenReturn(List.of());

        PostChunkRebuildService service = new PostChunkRebuildService(posts, projection);

        assertEquals(2, service.rebuildPublic(2));
        verify(projection, times(2)).apply(any(PostLifecycleEvent.class));
    }

    private EmbeddingService embedding() {
        EmbeddingService embedding = mock(EmbeddingService.class);
        when(embedding.dimension()).thenReturn(384);
        when(embedding.model()).thenReturn("multilingual");
        when(embedding.vectorVersion()).thenReturn("v1");
        when(embedding.embedDocument(any())).thenReturn(new float[]{1.0f, 0.0f, 0.0f});
        return embedding;
    }

    private RagProperties properties() {
        RagProperties properties = mock(RagProperties.class);
        when(properties.enabled()).thenReturn(true);
        when(properties.maxSourceBytes()).thenReturn(524_288);
        return properties;
    }

    private PostLifecycleEvent event(long postId, long version, PostLifecycleEventType type) {
        return new PostLifecycleEvent(1L, postId, version, type, "published", "public",
                null, null, null, Instant.now(), 1L, "test");
    }
}
