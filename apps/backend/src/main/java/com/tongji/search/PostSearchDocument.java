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
    public String textForEmbedding() {
        return String.join(" ",
                nullToEmpty(title),
                nullToEmpty(description),
                nullToEmpty(tags),
                nullToEmpty(content)).trim();
    }

    private static String nullToEmpty(String value) {
        return value == null ? "" : value;
    }
}
