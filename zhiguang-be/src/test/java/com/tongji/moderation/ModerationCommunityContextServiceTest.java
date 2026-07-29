package com.tongji.moderation;

import com.tongji.comment.mapper.CommentMapper;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.storage.OssStorageService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class ModerationCommunityContextServiceTest {
    private KnowPostMapper knowPostMapper;
    private OssStorageService storageService;
    private ModerationCommunityContextService service;

    @BeforeEach
    void setUp() {
        knowPostMapper = mock(KnowPostMapper.class);
        storageService = mock(OssStorageService.class);
        service = new ModerationCommunityContextService(
                knowPostMapper,
                mock(CommentMapper.class),
                storageService
        );
    }

    @Test
    void loadsRealPostBodyForModerationContext() {
        KnowPost post = post(42L, "reviewing", null);
        when(knowPostMapper.findById(42L)).thenReturn(post);
        when(storageService.readTextObject("posts/42.md", 512 * 1024))
                .thenReturn("真实帖子正文");

        var snapshot = service.getContentContext(42L);

        assertEquals("42", snapshot.current().contentId());
        assertEquals("POST", snapshot.current().contentType());
        assertEquals("真实帖子正文", snapshot.current().content());
        assertEquals(snapshot.current(), snapshot.post());
        assertFalse(snapshot.parentCommentRequired());
    }

    @Test
    void derivesViolationHistoryFromRejectedPosts() {
        KnowPost rejected = post(41L, "rejected", "包含广告引流");
        when(knowPostMapper.listRejectedForModeration(7L, 20))
                .thenReturn(List.of(rejected));

        var violations = service.getAuthorViolationHistory(7L);

        assertEquals(1, violations.size());
        assertEquals("ADVERTISING", violations.getFirst().riskType());
        assertEquals("REJECT", violations.getFirst().action());
        verify(knowPostMapper).listRejectedForModeration(7L, 20);
    }

    private KnowPost post(long id, String status, String reason) {
        return KnowPost.builder()
                .id(id)
                .creatorId(7L)
                .title("帖子标题")
                .description("帖子摘要")
                .contentObjectKey("posts/" + id + ".md")
                .status(status)
                .moderationReason(reason)
                .createTime(Instant.parse("2026-07-29T00:00:00Z"))
                .updateTime(Instant.parse("2026-07-29T00:10:00Z"))
                .build();
    }
}
