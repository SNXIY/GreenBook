package com.tongji.agentfacade.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.agentfacade.mapper.ScheduledPublicationMapper;
import com.tongji.agentfacade.mapper.ScheduledPublicationRecord;
import com.tongji.knowpost.id.SnowflakeIdGenerator;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.knowpost.service.KnowPostService;
import com.tongji.notification.service.NotificationService;
import com.tongji.relation.outbox.OutboxMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.Instant;
import java.util.List;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 定时/立即发布成功必须触发发布成功通知；
 * 通知失败绝不能破坏已成功发布的 schedule 状态机（不允许 markFailed）。
 */
@ExtendWith(MockitoExtension.class)
class ScheduledPublicationNotificationTest {

    @Mock private ScheduledPublicationMapper mapper;
    @Mock private KnowPostMapper knowPostMapper;
    @Mock private KnowPostService knowPostService;
    @Mock private SnowflakeIdGenerator idGen;
    @Mock private OutboxMapper outboxMapper;
    @Mock private ObjectMapper objectMapper;
    @Mock private NotificationService notificationService;

    @InjectMocks private ScheduledPublicationService service;

    private static final long SCHEDULE_ID = 100L;
    private static final long USER_ID = 1L;
    private static final long DRAFT_ID = 200L;

    private ScheduledPublicationRecord dueRecord() {
        return ScheduledPublicationRecord.builder()
                .id(SCHEDULE_ID)
                .userId(USER_ID)
                .draftId(DRAFT_ID)
                .status("SCHEDULED")
                .createdAt(Instant.now().minusSeconds(60))
                .updatedAt(Instant.now().minusSeconds(60))
                .build();
    }

    private KnowPost draftPost() {
        KnowPost post = new KnowPost();
        post.setId(DRAFT_ID);
        post.setCreatorId(USER_ID);
        post.setStatus("draft");
        post.setTitle("Java 后端实习面试 10 问");
        return post;
    }

    @Test
    void scheduledPublicationSuccess_notifiesUser() {
        when(mapper.findDue(any(), eq(10))).thenReturn(List.of(dueRecord()));
        when(mapper.claimForExecution(SCHEDULE_ID)).thenReturn(1);
        when(knowPostMapper.findById(DRAFT_ID)).thenReturn(draftPost());
        when(knowPostService.publish(USER_ID, DRAFT_ID)).thenReturn("published");
        when(mapper.markPublished(eq(SCHEDULE_ID), eq(DRAFT_ID), any())).thenReturn(1);

        service.executeDuePublications();

        verify(notificationService).notifyPostPublished(
                "publication.published:" + SCHEDULE_ID, USER_ID, DRAFT_ID, SCHEDULE_ID);
    }

    @Test
    void publishNowSuccess_notifiesUser() {
        when(knowPostMapper.findById(DRAFT_ID)).thenReturn(draftPost());
        when(knowPostService.publish(USER_ID, DRAFT_ID)).thenReturn("published");

        service.publishNow(USER_ID, DRAFT_ID);

        verify(notificationService).notifyPostPublished(
                "publication.published:now:" + DRAFT_ID, USER_ID, DRAFT_ID, null);
    }

    @Test
    void alreadyPublishedPublishNow_doesNotNotifyAgain() {
        KnowPost published = draftPost();
        published.setStatus("published");
        when(knowPostMapper.findById(DRAFT_ID)).thenReturn(published);

        service.publishNow(USER_ID, DRAFT_ID);

        verify(notificationService, never()).notifyPostPublished(anyString(), anyLong(), anyLong(), any());
    }

    @Test
    void notificationFailure_doesNotFailPublishedStateMachine() {
        when(mapper.findDue(any(), eq(10))).thenReturn(List.of(dueRecord()));
        when(mapper.claimForExecution(SCHEDULE_ID)).thenReturn(1);
        when(knowPostMapper.findById(DRAFT_ID)).thenReturn(draftPost());
        when(knowPostService.publish(USER_ID, DRAFT_ID)).thenReturn("published");
        when(mapper.markPublished(eq(SCHEDULE_ID), eq(DRAFT_ID), any())).thenReturn(1);
        doThrow(new RuntimeException("notify down"))
                .when(notificationService)
                .notifyPostPublished("publication.published:" + SCHEDULE_ID, USER_ID, DRAFT_ID, SCHEDULE_ID);

        service.executeDuePublications();

        // Publication stays PUBLISHED; notification failure must not markFailed.
        verify(mapper).markPublished(eq(SCHEDULE_ID), eq(DRAFT_ID), any());
        verify(mapper, never()).markFailed(anyLong(), anyString(), anyString());
    }
}
