package com.tongji.moderation;

import java.util.List;

public record ContentModerationResult(
        boolean rejected,
        boolean pending,
        List<String> hitWords,
        String reason
) {
    public static ContentModerationResult pass() {
        return new ContentModerationResult(false, false, List.of(), null);
    }

    public static ContentModerationResult reject(List<String> hitWords, String reason) {
        return new ContentModerationResult(true, false, hitWords == null ? List.of() : List.copyOf(hitWords), reason);
    }

    public static ContentModerationResult pending(String reason) {
        return new ContentModerationResult(false, true, List.of(), reason);
    }
}
