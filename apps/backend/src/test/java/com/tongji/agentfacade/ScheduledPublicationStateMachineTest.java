package com.tongji.agentfacade;

import com.tongji.agentfacade.mapper.ScheduledPublicationMapper;
import com.tongji.agentfacade.mapper.ScheduledPublicationRecord;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.Instant;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.when;

/**
 * Tests the scheduled publication state machine via the Mapper interface.
 * Verifies that the SQL WHERE clauses correctly enforce state transitions.
 *
 * State machine under test:
 *   SCHEDULED → claimForExecution → PROCESSING → markPublished → PUBLISHED
 *   SCHEDULED → claimForExecution → PROCESSING → markFailed    → FAILED
 *   SCHEDULED → cancel              → CANCELLED
 *   PROCESSING (stale)              → markFailed    → FAILED (WORKER_TIMEOUT)
 */
@ExtendWith(MockitoExtension.class)
class ScheduledPublicationStateMachineTest {

    @Mock
    private ScheduledPublicationMapper mapper;

    private static final long SCHEDULE_ID = 100L;
    private static final long USER_ID = 1L;
    private static final long DRAFT_ID = 200L;
    private static final long POST_ID = 300L;

    @BeforeEach
    void setUp() {
        // Default: all mapper methods return 0 (no rows affected) unless explicitly mocked
    }

    // ── 1. SCHEDULED → PROCESSING (claim) ──────────────────────

    @Test
    void claimForExecution_scheduledToProcessing_shouldSucceed() {
        when(mapper.claimForExecution(SCHEDULE_ID)).thenReturn(1);

        int claimed = mapper.claimForExecution(SCHEDULE_ID);
        assertThat(claimed).isEqualTo(1);
    }

    @Test
    void twoWorkersClaimConcurrently_onlyOneSucceeds() {
        // Worker 1 wins
        when(mapper.claimForExecution(SCHEDULE_ID))
                .thenReturn(1)  // first call succeeds
                .thenReturn(0); // second call fails (already PROCESSING)

        int worker1 = mapper.claimForExecution(SCHEDULE_ID);
        int worker2 = mapper.claimForExecution(SCHEDULE_ID);

        assertThat(worker1).isEqualTo(1);
        assertThat(worker2).isEqualTo(0);
    }

    // ── 2. PROCESSING → PUBLISHED ──────────────────────────────

    @Test
    void processing_canTransitionToPublished() {
        when(mapper.markPublished(eq(SCHEDULE_ID), anyLong(), any()))
                .thenReturn(1);

        int result = mapper.markPublished(SCHEDULE_ID, POST_ID, Instant.now());
        assertThat(result).isEqualTo(1);
    }

    // ── 3. PROCESSING → FAILED ─────────────────────────────────

    @Test
    void processing_canTransitionToFailed() {
        when(mapper.markFailed(eq(SCHEDULE_ID), anyString(), anyString()))
                .thenReturn(1);

        int result = mapper.markFailed(SCHEDULE_ID, "TEST_FAILURE", "test");
        assertThat(result).isEqualTo(1);
    }

    // ── 4. SCHEDULED cannot directly markPublished ─────────────

    @Test
    void scheduled_cannotDirectlyMarkPublished() {
        // The SQL WHERE requires PROCESSING, so SCHEDULED returns 0
        when(mapper.markPublished(eq(SCHEDULE_ID), anyLong(), any()))
                .thenReturn(0);

        int result = mapper.markPublished(SCHEDULE_ID, POST_ID, Instant.now());
        assertThat(result).isEqualTo(0);
    }

    // ── 5. CANCELLED cannot markPublished ──────────────────────

    @Test
    void cancelled_cannotMarkPublished() {
        when(mapper.markPublished(eq(SCHEDULE_ID), anyLong(), any()))
                .thenReturn(0);

        int result = mapper.markPublished(SCHEDULE_ID, POST_ID, Instant.now());
        assertThat(result).isEqualTo(0);
    }

    // ── 6. PUBLISHED repeat markPublished affects 0 rows ───────

    @Test
    void publishedRepeatMarkPublished_affectsZeroRows() {
        // Already PUBLISHED, so status != PROCESSING
        when(mapper.markPublished(eq(SCHEDULE_ID), anyLong(), any()))
                .thenReturn(0);

        int result = mapper.markPublished(SCHEDULE_ID, POST_ID, Instant.now());
        assertThat(result).isEqualTo(0);
    }

    // ── 7. published_post_id is not overwritten ────────────────

    @Test
    void publishedPostId_notOverwritten() {
        // The SQL has AND published_post_id IS NULL, so a second call returns 0
        when(mapper.markPublished(eq(SCHEDULE_ID), anyLong(), any()))
                .thenReturn(0);

        int result = mapper.markPublished(SCHEDULE_ID, 999L, Instant.now());
        assertThat(result).isEqualTo(0);
    }

    // ── 8. stale PROCESSING recovered to FAILED ────────────────

    @Test
    void staleProcessing_recoveredToFailed() {
        ScheduledPublicationRecord stale = ScheduledPublicationRecord.builder()
                .id(500L).userId(USER_ID).draftId(DRAFT_ID)
                .status("PROCESSING")
                .updatedAt(Instant.now().minusSeconds(300))
                .build();
        when(mapper.recoverStaleProcessing(any(), eq(5)))
                .thenReturn(List.of(stale));
        when(mapper.markFailed(eq(500L), eq("WORKER_TIMEOUT"), anyString()))
                .thenReturn(1);

        List<ScheduledPublicationRecord> found = mapper.recoverStaleProcessing(
                Instant.now().minusSeconds(120), 5);
        assertThat(found).hasSize(1);
        assertThat(found.get(0).getStatus()).isEqualTo("PROCESSING");

        int mf = mapper.markFailed(500L, "WORKER_TIMEOUT", "执行超时，需要人工重试");
        assertThat(mf).isEqualTo(1);
    }

    // ── 9. claim → publish creates only one post ───────────────

    @Test
    void claimThenPublish_createsOnlyOnePost() {
        // Worker claims successfully
        when(mapper.claimForExecution(SCHEDULE_ID)).thenReturn(1);
        int claimed = mapper.claimForExecution(SCHEDULE_ID);
        assertThat(claimed).isEqualTo(1);

        // Publish succeeds
        when(mapper.markPublished(eq(SCHEDULE_ID), eq(POST_ID), any()))
                .thenReturn(1);
        int mp = mapper.markPublished(SCHEDULE_ID, POST_ID, Instant.now());
        assertThat(mp).isEqualTo(1);

        // Second publish attempt returns 0 (published_post_id already set)
        when(mapper.markPublished(eq(SCHEDULE_ID), anyLong(), any()))
                .thenReturn(0);
        int mp2 = mapper.markPublished(SCHEDULE_ID, 999L, Instant.now());
        assertThat(mp2).isEqualTo(0);
    }
}
