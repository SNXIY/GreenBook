package com.tongji.agentfacade;

import com.tongji.agentfacade.api.dto.AgentDraftCreateRequest;
import com.tongji.agentfacade.service.AgentFacadeService;
import com.tongji.agentfacade.mapper.ScheduledPublicationMapper;
import com.tongji.comment.service.CommentService;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.counter.service.CounterService;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.relation.mapper.RelationMapper;
import com.tongji.storage.OssStorageService;
import jakarta.validation.Validation;
import jakarta.validation.Validator;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.*;

class DraftMetadataContractTest {

    private final Validator validator = Validation.buildDefaultValidatorFactory().getValidator();

    @Test
    void exactDescriptionLimitIsValid() {
        AgentDraftCreateRequest request = new AgentDraftCreateRequest(
                "Title", "# Body", "x".repeat(50), "public");

        assertTrue(validator.validate(request).isEmpty());
    }

    @Test
    void descriptionOverLimitIsRejectedByDto() {
        AgentDraftCreateRequest request = new AgentDraftCreateRequest(
                "Title", "# Body", "x".repeat(51), "public");

        assertFalse(validator.validate(request).isEmpty());
    }

    @Test
    void serviceRejectsBeforeCreatingDraft() {
        KnowPostMapper mapper = mock(KnowPostMapper.class);
        KnowPostService posts = mock(KnowPostService.class);
        AgentFacadeService service = new AgentFacadeService(
                mapper, posts, mock(CommentService.class), mock(CounterService.class),
                mock(RelationMapper.class), mock(OssStorageService.class),
                mock(ScheduledPublicationMapper.class));

        BusinessException error = assertThrows(BusinessException.class, () -> service.createDraft(
                1L, new AgentDraftCreateRequest("Title", "# Body", "x".repeat(51), "public")));

        assertEquals(ErrorCode.FIELD_TOO_LONG, error.getErrorCode());
        verifyNoInteractions(posts, mapper);
    }
}
