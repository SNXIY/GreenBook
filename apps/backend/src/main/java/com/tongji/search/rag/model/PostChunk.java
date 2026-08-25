package com.tongji.search.rag.model;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.Instant;

/** Rebuildable, post-traceable evidence unit. */
@Data
@NoArgsConstructor
@AllArgsConstructor
public class PostChunk {
    private String chunkId;
    private long postId;
    private int chunkIndex;
    private String content;
    private int tokenCount;
    private int startOffset;
    private int endOffset;
    private String embeddingModel;
    private String embeddingVersion;
    private int dimension;
    private long eventVersion;
    private Instant createdAt;
    private Instant updatedAt;

    public String textForEmbedding(String title, String tags, String description) {
        return "title: " + truncate(title, 256)
                + "\ntags: " + truncate(tags, 512)
                + "\ndescription: " + truncate(description, 768)
                + "\ncontent: " + (content == null ? "" : content);
    }

    private static String truncate(String value, int maxChars) {
        String normalized = value == null ? "" : value.trim();
        if (normalized.length() <= maxChars) return normalized;
        int end = normalized.offsetByCodePoints(0,
                normalized.codePointCount(0, maxChars));
        return normalized.substring(0, end);
    }
}
