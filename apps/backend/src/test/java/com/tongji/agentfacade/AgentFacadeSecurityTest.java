package com.tongji.agentfacade;

import com.tongji.agentfacade.api.dto.*;
import com.tongji.agentfacade.service.AgentFacadeService;
import com.tongji.agentfacade.service.IdempotencyService;
import com.tongji.agentfacade.service.ScheduledPublicationService;
import com.tongji.auth.token.JwtService;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.model.KnowPostDetailRow;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.comment.service.CommentService;
import com.tongji.counter.service.CounterService;
import com.tongji.relation.mapper.RelationMapper;
import com.tongji.storage.OssStorageService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class AgentFacadeSecurityTest {

    @Mock private KnowPostMapper knowPostMapper;
    @Mock private KnowPostService knowPostService;
    @Mock private CommentService commentService;
    @Mock private CounterService counterService;
    @Mock private OssStorageService ossStorageService;
    @Mock private RelationMapper relationMapper;

    private AgentFacadeService agentFacadeService;

    @BeforeEach
    void setUp() {
        agentFacadeService = new AgentFacadeService(
                knowPostMapper, knowPostService, commentService,
                counterService, relationMapper, ossStorageService);
    }

    @Test
    void getDraft_shouldRejectWhenCreatorIdDoesNotMatch() {
        KnowPost draft = KnowPost.builder()
                .id(100L).creatorId(2L).status("draft")
                .title("other draft").build();
        when(knowPostMapper.findById(100L)).thenReturn(draft);

        BusinessException ex = assertThrows(BusinessException.class, () ->
                agentFacadeService.getDraft(1L, 100L)); // user 1 tries to read user 2's draft
        assertTrue(ex.getMessage().contains("不属于当前用户"));
    }

    @Test
    void getDraft_shouldReturnWhenOwnerMatches() {
        KnowPost draft = KnowPost.builder()
                .id(100L).creatorId(1L).status("draft")
                .title("my draft").build();
        when(knowPostMapper.findById(100L)).thenReturn(draft);
        when(ossStorageService.readTextObject(nullable(String.class), anyInt())).thenReturn("");

        DraftResponse response = agentFacadeService.getDraft(1L, 100L);
        assertNotNull(response);
        assertEquals("100", response.draftId());
    }

    @Test
    void updateDraft_shouldRejectWhenNotOwner() {
        KnowPost draft = KnowPost.builder()
                .id(100L).creatorId(2L).status("draft").build();
        when(knowPostMapper.findById(100L)).thenReturn(draft);

        AgentDraftUpdateRequest request = new AgentDraftUpdateRequest("title", "content", null, null, null, null);
        BusinessException ex = assertThrows(BusinessException.class, () ->
                agentFacadeService.updateDraft(1L, 100L, request));
        assertTrue(ex.getMessage().contains("不属于当前用户"));
    }

    @Test
    void updateDraft_shouldRejectWhenStatusIsNotDraft() {
        KnowPost post = KnowPost.builder()
                .id(100L).creatorId(1L).status("published").build();
        when(knowPostMapper.findById(100L)).thenReturn(post);

        AgentDraftUpdateRequest request = new AgentDraftUpdateRequest("title", null, null, null, null, null);
        BusinessException ex = assertThrows(BusinessException.class, () ->
                agentFacadeService.updateDraft(1L, 100L, request));
        assertTrue(ex.getMessage().contains("只能修改草稿"));
    }
}
