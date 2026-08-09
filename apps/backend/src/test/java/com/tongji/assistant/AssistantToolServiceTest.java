package com.tongji.assistant;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.assistant.api.dto.AssistantPublishResponse;
import com.tongji.assistant.mapper.AssistantCommentProvenanceMapper;
import com.tongji.assistant.service.AssistantToolService;
import com.tongji.comment.service.CommentService;
import com.tongji.common.exception.BusinessException;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.storage.OssStorageService;
import com.tongji.user.mapper.UserMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.*;

class AssistantToolServiceTest {
    private static final String CONTENT_SHA = "a".repeat(64);

    private KnowPostMapper mapper;
    private KnowPostService knowPostService;
    private OssStorageService storage;
    private AssistantToolService service;

    @BeforeEach
    void setUp() {
        mapper = mock(KnowPostMapper.class);
        knowPostService = mock(KnowPostService.class);
        storage = mock(OssStorageService.class);
        service = new AssistantToolService(
                mapper,
                knowPostService,
                storage,
                new ObjectMapper(),
                mock(CommentService.class),
                mock(AssistantCommentProvenanceMapper.class),
                mock(UserMapper.class)
        );
    }

    @Test
    void searchOnlyReturnsMapperPublicResults() {
        KnowPostDetailRow row = new KnowPostDetailRow();
        row.setId(12L);
        row.setCreatorId(7L);
        row.setTitle("Java 学习路线");
        row.setDescription("从语法到项目");
        row.setTags("[\"Java\",\"学习\"]");
        row.setAuthorNickname("知光用户");
        row.setPublishTime(Instant.parse("2026-07-28T00:00:00Z"));
        when(mapper.searchPublicForAssistant("Java", 5)).thenReturn(List.of(row));

        var results = service.search(" Java ", 5);

        assertEquals(1, results.size());
        assertEquals(List.of("Java", "学习"), results.getFirst().tags());
        verify(mapper).searchPublicForAssistant("Java", 5);
    }

    @Test
    void serviceCanReplayAlreadyPublishedAiDraft() {
        KnowPost post = KnowPost.builder()
                .id(99L)
                .creatorId(7L)
                .contentOrigin("AI_ASSISTED")
                .contentSha256(CONTENT_SHA)
                .status("published")
                .build();
        when(mapper.findById(99L)).thenReturn(post);

        AssistantPublishResponse response = service.publishAiDraft(
                99L, 7L, CONTENT_SHA
        );

        assertEquals("published", response.status());
        assertEquals(true, response.replayed());
        verifyNoInteractions(knowPostService);
    }

    @Test
    void serviceRejectsManualDraft() {
        KnowPost post = KnowPost.builder()
                .id(99L)
                .creatorId(7L)
                .contentOrigin("MANUAL")
                .status("draft")
                .build();
        when(mapper.findById(99L)).thenReturn(post);

        assertThrows(
                BusinessException.class,
                () -> service.publishAiDraft(99L, 7L, CONTENT_SHA)
        );
        verifyNoInteractions(knowPostService);
    }

    @Test
    void serviceRejectsChangedDraftAfterApproval() {
        KnowPost post = KnowPost.builder()
                .id(99L)
                .creatorId(7L)
                .contentOrigin("AI_ASSISTED")
                .contentSha256("b".repeat(64))
                .status("draft")
                .build();
        when(mapper.findById(99L)).thenReturn(post);

        assertThrows(
                BusinessException.class,
                () -> service.publishAiDraft(99L, 7L, CONTENT_SHA)
        );
        verifyNoInteractions(knowPostService);
    }
}
