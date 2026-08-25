package com.tongji.search.rag.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/** Configuration owned by the evidence pipeline, separate from post search. */
@Component
public class RagProperties {
    @Value("${rag.enabled:true}")
    private boolean enabled;
    @Value("${rag.qdrant.collection:post_chunks_multilingual_v1}")
    private String qdrantCollection;
    @Value("${rag.chunk.max-chars:1200}")
    private int chunkMaxChars;
    @Value("${rag.chunk.overlap-chars:160}")
    private int chunkOverlapChars;
    @Value("${rag.chunk.max-source-bytes:524288}")
    private int maxSourceBytes;
    @Value("${rag.retrieval.candidate-posts:8}")
    private int candidatePosts;
    @Value("${rag.retrieval.top-chunks:8}")
    private int topChunks;

    public boolean enabled() { return enabled; }
    public String qdrantCollection() { return qdrantCollection; }
    public int chunkMaxChars() { return chunkMaxChars; }
    public int chunkOverlapChars() { return chunkOverlapChars; }
    public int maxSourceBytes() { return maxSourceBytes; }
    public int candidatePosts() { return candidatePosts; }
    public int topChunks() { return topChunks; }
}
