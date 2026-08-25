package com.tongji.search.rag.model;

/** Qdrant result metadata; content is loaded from the MySQL chunk projection. */
public record ChunkDenseSearchHit(
        String chunkId,
        long postId,
        double score,
        long eventVersion,
        int chunkIndex,
        int startOffset,
        int endOffset
) {}
