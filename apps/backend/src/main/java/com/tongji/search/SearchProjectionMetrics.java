package com.tongji.search;

import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.Instant;
import java.util.concurrent.atomic.AtomicLong;

/** Small in-process observability surface for projection health and lag. */
@Component
public class SearchProjectionMetrics {
    private final AtomicLong esApplied = new AtomicLong();
    private final AtomicLong qdrantApplied = new AtomicLong();
    private final AtomicLong staleEvents = new AtomicLong();
    private final AtomicLong missingProjection = new AtomicLong();
    private final AtomicLong consumerFailures = new AtomicLong();
    private final AtomicLong deleteApplied = new AtomicLong();
    private volatile long lastLagMs;

    public void appliedEs(Instant updatedAt) { esApplied.incrementAndGet(); recordLag(updatedAt); }
    public void appliedQdrant(Instant updatedAt) { qdrantApplied.incrementAndGet(); recordLag(updatedAt); }
    public void deleted() { deleteApplied.incrementAndGet(); }
    public void stale() { staleEvents.incrementAndGet(); }
    public void missing() { missingProjection.incrementAndGet(); }
    public void failure() { consumerFailures.incrementAndGet(); }

    private void recordLag(Instant updatedAt) {
        if (updatedAt != null) {
            lastLagMs = Math.max(0L, Duration.between(updatedAt, Instant.now()).toMillis());
        }
    }

    public Snapshot snapshot() {
        return new Snapshot(esApplied.get(), qdrantApplied.get(), staleEvents.get(),
                missingProjection.get(), consumerFailures.get(), deleteApplied.get(), lastLagMs);
    }

    public record Snapshot(long esApplied, long qdrantApplied, long staleEvents,
                           long missingProjection, long consumerFailures,
                           long deleteApplied, long lastLagMs) {}
}
