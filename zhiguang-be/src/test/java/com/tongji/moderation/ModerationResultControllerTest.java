package com.tongji.moderation;

import com.tongji.knowpost.service.KnowPostService;
import com.tongji.moderation.api.ModerationResultController;
import com.tongji.moderation.api.ModerationResultRequest;
import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;

class ModerationResultControllerTest {

    @Test
    void acceptsSharedSecretAndAppliesResult() {
        KnowPostService service = mock(KnowPostService.class);
        ModerationResultController controller =
                new ModerationResultController(service, "shared-secret");
        ModerationResultRequest request =
                new ModerationResultRequest("42", "COMPLETED", "PASS", "审核通过");

        controller.applyResult("task-id", "shared-secret", request);

        verify(service).applyModerationResult(
                "task-id", "42", "COMPLETED", "PASS", "审核通过"
        );
    }

    @Test
    void rejectsMissingOrWrongSharedSecret() {
        KnowPostService service = mock(KnowPostService.class);
        ModerationResultController controller =
                new ModerationResultController(service, "shared-secret");
        ModerationResultRequest request =
                new ModerationResultRequest("42", "COMPLETED", "PASS", null);

        assertThrows(
                AccessDeniedException.class,
                () -> controller.applyResult("task-id", null, request)
        );
        assertThrows(
                AccessDeniedException.class,
                () -> controller.applyResult("task-id", "wrong", request)
        );
        verifyNoInteractions(service);
    }
}
