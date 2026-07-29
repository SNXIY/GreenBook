package com.tongji.moderation;

import com.tongji.moderation.api.ModerationCommunityController;
import com.tongji.moderation.api.dto.ModerationCommunityContentRecord;
import com.tongji.moderation.api.dto.ModerationCommunityContentSnapshot;
import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class ModerationCommunityControllerTest {

    @Test
    void requiresSharedSecretBeforeReadingContext() {
        ModerationCommunityContextService service =
                mock(ModerationCommunityContextService.class);
        ModerationCommunityController controller =
                new ModerationCommunityController(service, "shared-secret");
        ModerationCommunityContentRecord record =
                new ModerationCommunityContentRecord(
                        "42", "POST", "7", "正文", "标题",
                        "reviewing", null
                );
        when(service.getContentContext(42L))
                .thenReturn(new ModerationCommunityContentSnapshot(record, record, false));

        assertThrows(
                AccessDeniedException.class,
                () -> controller.getContentContext(42L, "wrong")
        );
        var result = controller.getContentContext(42L, "shared-secret");

        assertEquals("42", result.current().contentId());
        verify(service).getContentContext(42L);
    }
}
