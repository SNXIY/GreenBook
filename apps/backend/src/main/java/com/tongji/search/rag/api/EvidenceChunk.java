package com.tongji.search.rag.api;

import java.time.Instant;

/** Evidence returned to the answer capability; no answer is generated here. */
public record EvidenceChunk(
        String chunkId,
        String postId,
        String title,
        String content,
        double score,
        int startOffset,
        int endOffset,
        long eventVersion,
        Instant updatedAt
) {}
