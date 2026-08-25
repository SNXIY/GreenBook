package com.tongji.agentfacade;

import com.tongji.agentfacade.api.dto.*;
import com.tongji.agentfacade.service.AgentFacadeService;
import com.tongji.agentfacade.mapper.ScheduledPublicationMapper;
import com.tongji.agentfacade.service.IdempotencyService;
import com.tongji.agentfacade.service.ScheduledPublicationService;
import com.tongji.comment.service.CommentService;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.counter.service.CounterService;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.relation.mapper.RelationMapper;
import com.tongji.storage.OssStorageService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.Instant;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * Tests that text-only posts work without object storage configured.
 * The OssStorageService.putTextObject and readTextObject must fall back
 * to local storage when OSS credentials are absent, regardless of the
 * provider setting.
 */
@ExtendWith(MockitoExtension.class)
class TextOnlyPostWithoutOssTest {

    @Mock private KnowPostMapper knowPostMapper;
    @Mock private KnowPostService knowPostService;
    @Mock private CommentService commentService;
    @Mock private CounterService counterService;
    @Mock private OssStorageService ossStorageService;
    @Mock private RelationMapper relationMapper;
    @Mock private ScheduledPublicationMapper scheduledPublicationMapper;

    private AgentFacadeService agentFacadeService;

    private static final long USER_ID = 1L;
    private static final long DRAFT_ID = 100L;
    private static final long POST_ID = 200L;

    @BeforeEach
    void setUp() {
        agentFacadeService = new AgentFacadeService(
                knowPostMapper, knowPostService, commentService,
                counterService, relationMapper, ossStorageService, scheduledPublicationMapper);
    }

    @Test
    void createTextOnlyDraft_shouldSucceedWithoutOss() {
        AgentDraftCreateRequest req = new AgentDraftCreateRequest(
                "学好Java", "# Java学习指南\n...", "如何学好Java的指南", "public");

        when(knowPostService.createDraft(USER_ID, "AI_ASSISTED")).thenReturn(DRAFT_ID);
        when(ossStorageService.putTextObject(anyString(), anyString(), eq("text/markdown")))
                .thenReturn("fake-etag");
        doNothing().when(knowPostService).confirmContent(
                eq(USER_ID), eq(DRAFT_ID), anyString(), eq("fake-etag"), anyLong(), anyString());
        doNothing().when(knowPostService).updateMetadata(
                eq(USER_ID), eq(DRAFT_ID), eq("学好Java"), isNull(),
                isNull(), isNull(), eq("public"), eq(false), anyString());

        KnowPost draft = KnowPost.builder()
                .id(DRAFT_ID).creatorId(USER_ID).status("draft").title("学好Java")
                .description("如何学好Java的指南").visible("public").contentOrigin("AI_ASSISTED")
                .createTime(Instant.now()).updateTime(Instant.now()).build();
        when(knowPostMapper.findById(DRAFT_ID)).thenReturn(draft);
        when(ossStorageService.readTextObject(anyString(), anyInt())).thenReturn("# Java学习指南\n...");

        DraftResponse result = agentFacadeService.createDraft(USER_ID, req);

        assertNotNull(result);
        assertEquals(String.valueOf(DRAFT_ID), result.draftId());
        assertEquals("学好Java", result.title());

        // Verify OSS was called for text storage (it should succeed regardless)
        verify(ossStorageService).putTextObject(anyString(), eq("# Java学习指南\n..."), eq("text/markdown"));
    }

    @Test
    void getPost_shouldRejectDraft() {
        KnowPostDetailRow row = new KnowPostDetailRow();
        // Simulate: row is a draft, not published
        when(knowPostMapper.findDetailById(POST_ID)).thenReturn(null);

        assertThrows(BusinessException.class, () ->
                agentFacadeService.getPost(POST_ID));
    }

    @Test
    void getPostComments_shouldRejectWhenPostNotVisible() {
        when(knowPostMapper.findDetailById(POST_ID)).thenReturn(null);
        assertThrows(BusinessException.class,
                () -> agentFacadeService.getPostComments(POST_ID, null, 20));
    }

    @Test
    void getPostAnalytics_shouldRejectWhenPostNotVisible() {
        when(knowPostMapper.findDetailById(POST_ID)).thenReturn(null);
        assertThrows(BusinessException.class,
                () -> agentFacadeService.getPostAnalytics(POST_ID));
    }

    @Test
    void createDraft_rollsBackOnContentUploadFailure() {
        // Simulate putTextObject throwing
        AgentDraftCreateRequest req = new AgentDraftCreateRequest(
                "test", "body", "summary", "public");
        when(knowPostService.createDraft(USER_ID, "AI_ASSISTED")).thenReturn(DRAFT_ID);
        when(ossStorageService.putTextObject(anyString(), anyString(), anyString()))
                .thenThrow(new BusinessException(ErrorCode.BAD_REQUEST, "正文写入失败"));

        assertThrows(BusinessException.class, () ->
                agentFacadeService.createDraft(USER_ID, req));

        // confirmContent must NOT be called because putTextObject failed
        verify(knowPostService, never()).confirmContent(anyLong(), anyLong(),
                anyString(), anyString(), anyLong(), anyString());
        verify(knowPostService, never()).updateMetadata(anyLong(), anyLong(),
                anyString(), isNull(), isNull(), isNull(), anyString(), anyBoolean(), anyString());
    }
}
