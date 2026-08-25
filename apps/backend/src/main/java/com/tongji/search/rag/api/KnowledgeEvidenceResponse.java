package com.tongji.search.rag.api;

import java.util.List;

public record KnowledgeEvidenceResponse(
        List<EvidenceChunk> chunks,
        int candidatePostCount,
        long embeddingLatencyMs,
        long chunkRetrievalLatencyMs,
        boolean degraded
) {}
