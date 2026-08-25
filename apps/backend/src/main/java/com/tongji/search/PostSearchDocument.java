package com.tongji.search;

import java.time.Instant;

/** Projection document; MySQL remains the authoritative source for every field. */
public record PostSearchDocument(
        long postId,
        Long creatorId,
        String title,
        String description,
        String tags,
        String content,
        String status,
        String visibility,
        Instant publishTime,
        Instant updatedAt,
        long eventVersion
) {
    private static final int MAX_TITLE_CHARS = 512;
    private static final int MAX_TAG_CHARS = 768;
    private static final int MAX_DESCRIPTION_CHARS = 1_024;
    private static final int MAX_CONTENT_CHARS = 4_096;

    /**
     * Bounded, field-labelled representation for a post-level embedding.
     * Projection still keeps the canonical content for ES; dense indexing does
     * not send an entire large object to the encoder.
     */
    public String textForEmbedding() {
        return "title: " + truncate(title, MAX_TITLE_CHARS)
                + "\ntags: " + truncate(tags, MAX_TAG_CHARS)
                + "\ndescription: " + truncate(description, MAX_DESCRIPTION_CHARS)
                + "\ncontent: " + truncate(content, MAX_CONTENT_CHARS);
    }

    private static String nullToEmpty(String value) {
        return value == null ? "" : value;
    }

    private static String truncate(String value, int maxChars) {
        String normalized = nullToEmpty(value).trim();
        if (normalized.length() <= maxChars) return normalized;
        int end = normalized.offsetByCodePoints(0, normalized.codePointCount(0, maxChars));
        return normalized.substring(0, end);
    }
}
