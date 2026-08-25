package com.tongji.search;

import com.tongji.agentfacade.api.dto.SearchPageResponse;
import com.tongji.agentfacade.api.dto.SearchPostItem;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.search.config.SearchProperties;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.Mockito.*;

class HybridSearchServiceTest {
    @Test
    void fusesLexicalAndDenseRanksWhileKeepingDomainResponse() {
        MySqlSearchProvider mysql = mock(MySqlSearchProvider.class);
        ElasticsearchPostClient es = mock(ElasticsearchPostClient.class);
        QdrantPostClient qdrant = mock(QdrantPostClient.class);
        EmbeddingService embedding = mock(EmbeddingService.class);
        SearchProperties properties = mock(SearchProperties.class);
        when(properties.hybridEnabled()).thenReturn(true);
        when(properties.candidateLimit()).thenReturn(10);
        when(properties.requestTimeoutMs()).thenReturn(2500);
        when(embedding.embed("java" + "" )).thenReturn(new float[] {1.0f});
        when(es.search("java", 10)).thenReturn(List.of(
                new LexicalSearchHit(1L, 10.0, 1),
                new LexicalSearchHit(2L, 8.0, 2)));
        when(qdrant.search(any(), eq(10))).thenReturn(List.of(
                new DenseSearchHit(1L, 0.9, 1),
                new DenseSearchHit(2L, 0.8, 2)));
        when(mysql.count("java")).thenReturn(2L);
        when(mysql.loadPublicByIds(any())).thenReturn(List.of(row(1L), row(2L)));
        when(mysql.toItem(any())).thenAnswer(invocation -> item(((KnowPostDetailRow) invocation.getArgument(0)).getId()));

        HybridSearchService service = new HybridSearchService(mysql, es, qdrant, embedding,
                properties, new SearchProjectionMetrics());
        SearchPageResponse response = service.search("java", "relevant", 1, 2);

        assertEquals("hybrid_rrf", response.provider());
        assertEquals(false, response.degraded());
        assertEquals("1", response.items().get(0).postId());
        verify(qdrant).search(any(), eq(10));
    }

    @Test
    void providerFailureIsObservableMysqlFallback() {
        MySqlSearchProvider mysql = mock(MySqlSearchProvider.class);
        ElasticsearchPostClient es = mock(ElasticsearchPostClient.class);
        QdrantPostClient qdrant = mock(QdrantPostClient.class);
        EmbeddingService embedding = mock(EmbeddingService.class);
        SearchProperties properties = mock(SearchProperties.class);
        when(properties.hybridEnabled()).thenReturn(true);
        when(properties.candidateLimit()).thenReturn(10);
        when(properties.requestTimeoutMs()).thenReturn(2500);
        when(es.search("java", 10)).thenThrow(new SearchProviderUnavailableException("down"));
        SearchPageResponse baseline = new SearchPageResponse(List.of(), 1, 10, 0, 0,
                false, "relevant", "mysql", false);
        when(mysql.search("java", "relevant", 1, 10)).thenReturn(baseline);

        HybridSearchService service = new HybridSearchService(mysql, es, qdrant, embedding,
                properties, new SearchProjectionMetrics());
        SearchPageResponse response = service.search("java", "relevant", 1, 10);

        assertEquals("mysql_fallback", response.provider());
        assertEquals(true, response.degraded());
        verifyNoInteractions(qdrant);
    }

    private KnowPostDetailRow row(long id) {
        KnowPostDetailRow row = new KnowPostDetailRow();
        row.setId(id);
        row.setCreatorId(2L);
        row.setTitle("title");
        row.setPublishTime(Instant.parse("2026-08-25T00:00:00Z"));
        return row;
    }

    private SearchPostItem item(long id) {
        return new SearchPostItem(String.valueOf(id), "2", "title", "summary", List.of(),
                0L, 0L, 0L, Instant.parse("2026-08-25T00:00:00Z"), 0.0);
    }
}
