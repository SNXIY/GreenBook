package com.tongji.search.rag;

import com.tongji.agentfacade.api.dto.SearchPageResponse;
import com.tongji.agentfacade.api.dto.SearchPostItem;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.search.EmbeddingService;
import com.tongji.search.HybridSearchService;
import com.tongji.search.rag.api.KnowledgeEvidenceResponse;
import com.tongji.search.rag.config.RagProperties;
import com.tongji.search.rag.mapper.PostChunkMapper;
import com.tongji.search.rag.model.ChunkDenseSearchHit;
import com.tongji.search.rag.model.PostChunk;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.*;

class EvidenceRetrievalServiceTest {
    @Test
    void retrievesOnlyCurrentPublicChunksFromHybridCandidates() {
        HybridSearchService hybrid = mock(HybridSearchService.class);
        QdrantChunkClient qdrant = mock(QdrantChunkClient.class);
        PostChunkMapper chunks = mock(PostChunkMapper.class);
        KnowPostMapper posts = mock(KnowPostMapper.class);
        EmbeddingService embedding = mock(EmbeddingService.class);
        RagProperties properties = mock(RagProperties.class);
        when(properties.enabled()).thenReturn(true);
        when(properties.candidatePosts()).thenReturn(2);
        when(properties.topChunks()).thenReturn(2);
        when(embedding.embedQuery("如何部署 Java 服务")).thenReturn(new float[]{1.0f, 0.0f});
        when(hybrid.search("如何部署 Java 服务", "relevant", 1, 2)).thenReturn(
                new SearchPageResponse(List.of(
                        item("101", "Java deployment"),
                        item("102", "Docker deployment")),
                        1, 2, 2, 1, false, "relevant", "hybrid_rrf", false));
        String firstId = "chunk-101";
        String secondId = "chunk-102";
        when(qdrant.search(any(), eq(2), eq(List.of(101L, 102L)))).thenReturn(List.of(
                new ChunkDenseSearchHit(secondId, 102L, 0.95, 1, 0, 0, 10),
                new ChunkDenseSearchHit(firstId, 101L, 0.90, 1, 0, 0, 10)));
        when(chunks.findByIds(anyList())).thenReturn(List.of(
                row(firstId, 101L, "Java uses a release command."),
                row(secondId, 102L, "Docker packages the service.")));
        when(posts.findById(101L)).thenReturn(publicPost(101L, "Java deployment"));
        when(posts.findById(102L)).thenReturn(publicPost(102L, "Docker deployment"));

        EvidenceRetrievalService service = new EvidenceRetrievalService(
                hybrid, qdrant, chunks, posts, embedding, properties);
        KnowledgeEvidenceResponse response = service.retrieve("如何部署 Java 服务", 2, 2);

        assertEquals(2, response.chunks().size());
        assertEquals(secondId, response.chunks().get(0).chunkId());
        assertEquals("102", response.chunks().get(0).postId());
        verify(qdrant).search(any(), eq(2), eq(List.of(101L, 102L)));
    }

    private SearchPostItem item(String id, String title) {
        return new SearchPostItem(id, "1", title, "", List.of(), 0L, 0L, 0L,
                Instant.parse("2026-08-25T00:00:00Z"), 0.0);
    }

    private PostChunk row(String id, long postId, String content) {
        return new PostChunk(id, postId, 0, content, 4, 0, content.length(),
                "model", "v1", 384, 1L, Instant.now(), Instant.now());
    }

    private KnowPost publicPost(long id, String title) {
        return KnowPost.builder().id(id).title(title).status("published")
                .visible("public").eventVersion(1L).build();
    }
}
