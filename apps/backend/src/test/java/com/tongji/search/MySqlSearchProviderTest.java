package com.tongji.search;

import com.tongji.comment.api.dto.CommentPageResponse;
import com.tongji.comment.service.CommentService;
import com.tongji.counter.service.CounterService;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.search.config.SearchProperties;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.*;

class MySqlSearchProviderTest {
    @Test
    void usesOffsetAndDatabaseTotalForPagedLatestSearch() {
        KnowPostMapper mapper = mock(KnowPostMapper.class);
        CounterService counters = mock(CounterService.class);
        CommentService comments = mock(CommentService.class);
        SearchProperties properties = mock(SearchProperties.class);
        when(properties.maxHotCandidates()).thenReturn(10_000);
        when(mapper.countPublicForAgent("")).thenReturn(5L);
        when(mapper.searchPublicForAgent("", 5, 0)).thenReturn(List.of(
                row(1L), row(2L), row(3L), row(4L), row(5L)));
        when(counters.getCounts(any(), any(), anyList())).thenReturn(Map.of("like", 2L, "fav", 1L));
        when(comments.list(anyLong(), any(), any(), anyInt(), any())).thenReturn(
                new CommentPageResponse(List.of(), null, false));

        MySqlSearchProvider provider = new MySqlSearchProvider(mapper, counters, comments, properties);
        var response = provider.search("", "latest", 2, 2);

        verify(mapper).searchPublicForAgent("", 5, 0);
        assertEquals(5L, response.total());
        assertEquals(3, response.totalPages());
        assertEquals(2, response.items().size());
        assertEquals("mysql", response.provider());
        assertEquals(false, response.degraded());
    }

    @Test
    void tokenSearchKeepsTheSamePageContract() {
        KnowPostMapper mapper = mock(KnowPostMapper.class);
        CounterService counters = mock(CounterService.class);
        CommentService comments = mock(CommentService.class);
        SearchProperties properties = mock(SearchProperties.class);
        when(mapper.countPublicForAgentTokens(eq("java backend"), anyList())).thenReturn(8L);
        when(mapper.searchPublicForAgentTokens(eq("java backend"), anyList(), eq(3), eq(3)))
                .thenReturn(List.of(row(4L), row(5L)));
        when(counters.getCounts(any(), any(), anyList())).thenReturn(Map.of());
        when(comments.list(anyLong(), any(), any(), anyInt(), any())).thenReturn(
                new CommentPageResponse(List.of(), null, false));

        MySqlSearchProvider provider = new MySqlSearchProvider(mapper, counters, comments, properties);
        var response = provider.search("java backend", "relevant", 2, 3);

        verify(mapper).searchPublicForAgentTokens(eq("java backend"), anyList(), eq(3), eq(3));
        assertEquals(8L, response.total());
        assertEquals(3, response.totalPages());
    }

    private KnowPostDetailRow row(long id) {
        KnowPostDetailRow row = new KnowPostDetailRow();
        row.setId(id);
        row.setCreatorId(10L);
        row.setTitle("Post " + id);
        row.setDescription("description");
        row.setTags("[\"java\"]");
        row.setStatus("published");
        row.setVisible("public");
        row.setPublishTime(Instant.parse("2026-08-25T00:00:00Z"));
        return row;
    }
}
