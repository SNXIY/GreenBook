package com.tongji.search.rag;

import java.util.concurrent.atomic.AtomicLong;

/** Small in-process counters for focused projection and recovery checks. */
public class RagProjectionMetrics {
    private final AtomicLong applied = new AtomicLong();
    private final AtomicLong stale = new AtomicLong();
    private final AtomicLong deleted = new AtomicLong();
    private final AtomicLong failures = new AtomicLong();

    public void applied() { applied.incrementAndGet(); }
    public void stale() { stale.incrementAndGet(); }
    public void deleted() { deleted.incrementAndGet(); }
    public void failure() { failures.incrementAndGet(); }
    public long appliedCount() { return applied.get(); }
    public long staleCount() { return stale.get(); }
    public long deletedCount() { return deleted.get(); }
    public long failureCount() { return failures.get(); }
}
