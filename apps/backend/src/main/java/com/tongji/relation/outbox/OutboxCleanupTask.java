package com.tongji.relation.outbox;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * Periodically removes published outbox rows after the replay window.
 */
@Component
public class OutboxCleanupTask {
    private static final Logger log = LoggerFactory.getLogger(OutboxCleanupTask.class);

    private final OutboxMapper outboxMapper;
    private final boolean enabled;
    private final int retentionDays;
    private final int batchSize;

    public OutboxCleanupTask(OutboxMapper outboxMapper,
                             @Value("${outbox.cleanup.enabled:true}") boolean enabled,
                             @Value("${outbox.cleanup.retention-days:7}") int retentionDays,
                             @Value("${outbox.cleanup.batch-size:1000}") int batchSize) {
        this.outboxMapper = outboxMapper;
        this.enabled = enabled;
        this.retentionDays = retentionDays;
        this.batchSize = batchSize;
    }

    @Scheduled(fixedDelayString = "${outbox.cleanup.fixed-delay-ms:3600000}",
            initialDelayString = "${outbox.cleanup.initial-delay-ms:60000}")
    public void cleanup() {
        if (!enabled) {
            return;
        }

        int safeRetentionDays = Math.max(retentionDays, 1);
        int safeBatchSize = Math.min(Math.max(batchSize, 1), 5000);
        int deleted = outboxMapper.deletePublishedBefore(safeRetentionDays, safeBatchSize);
        if (deleted > 0) {
            log.info("Outbox cleanup deleted {} rows, retentionDays={} batchSize={}",
                    deleted, safeRetentionDays, safeBatchSize);
        }
    }
}
