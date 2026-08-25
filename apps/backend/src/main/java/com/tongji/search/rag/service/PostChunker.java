package com.tongji.search.rag.service;

import com.tongji.search.rag.config.RagProperties;
import com.tongji.search.rag.model.PostChunk;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/**
 * Small deterministic paragraph-first splitter. Offsets are Java UTF-16
 * offsets into the canonical source string, which makes citation lookup
 * reproducible without copying the source into the Qdrant payload.
 */
@Service
public class PostChunker {
    private final int maxChars;
    private final int overlapChars;

    @Autowired
    public PostChunker(RagProperties properties) {
        this(properties.chunkMaxChars(), properties.chunkOverlapChars());
    }

    public PostChunker(int maxChars, int overlapChars) {
        this.maxChars = Math.max(64, maxChars);
        this.overlapChars = Math.max(0, Math.min(overlapChars, this.maxChars / 2));
    }

    public List<PostChunk> chunk(long postId,
                                 long eventVersion,
                                 String content,
                                 String embeddingModel,
                                 String embeddingVersion,
                                 int dimension) {
        if (content == null || content.isBlank()) return List.of();
        List<int[]> paragraphs = paragraphRanges(content);
        List<PostChunk> result = new ArrayList<>();
        int chunkIndex = 0;
        Instant now = Instant.now();
        for (int[] paragraph : paragraphs) {
            int start = paragraph[0];
            int end = paragraph[1];
            while (start < end) {
                int windowEnd = safeEnd(content, start, Math.min(end, start + maxChars));
                int[] trimmed = trimRange(content, start, windowEnd);
                if (trimmed[0] < trimmed[1]) {
                    String chunkContent = content.substring(trimmed[0], trimmed[1]);
                    result.add(new PostChunk(
                            stableChunkId(postId, chunkIndex),
                            postId,
                            chunkIndex++,
                            chunkContent,
                            approximateTokenCount(chunkContent),
                            trimmed[0],
                            trimmed[1],
                            embeddingModel,
                            embeddingVersion,
                            dimension,
                            eventVersion,
                            now,
                            now
                    ));
                }
                if (windowEnd >= end) break;
                int next = Math.max(start + 1, windowEnd - overlapChars);
                start = safeStart(content, next, end);
            }
        }
        return List.copyOf(result);
    }

    private List<int[]> paragraphRanges(String content) {
        List<int[]> result = new ArrayList<>();
        int paragraphStart = 0;
        int index = 0;
        while (index < content.length()) {
            if (content.charAt(index) == '\n') {
                int runStart = index;
                int newlineCount = 0;
                while (index < content.length()) {
                    char c = content.charAt(index);
                    if (c == '\r' || c == '\n' || Character.isWhitespace(c)) {
                        if (c == '\n') newlineCount++;
                        index++;
                    } else {
                        break;
                    }
                }
                if (newlineCount >= 2) {
                    addTrimmed(result, content, paragraphStart, runStart);
                    paragraphStart = index;
                }
                continue;
            }
            index++;
        }
        addTrimmed(result, content, paragraphStart, content.length());
        return result;
    }

    private void addTrimmed(List<int[]> result, String content, int start, int end) {
        int[] range = trimRange(content, start, end);
        if (range[0] < range[1]) result.add(range);
    }

    private int[] trimRange(String content, int start, int end) {
        while (start < end && Character.isWhitespace(content.charAt(start))) start++;
        while (end > start && Character.isWhitespace(content.charAt(end - 1))) end--;
        return new int[]{start, end};
    }

    private int safeEnd(String text, int start, int candidate) {
        if (candidate <= start || candidate >= text.length()) return Math.min(candidate, text.length());
        return Character.isLowSurrogate(text.charAt(candidate)) ? candidate - 1 : candidate;
    }

    private int safeStart(String text, int candidate, int max) {
        int value = Math.min(candidate, max);
        if (value > 0 && value < text.length() && Character.isLowSurrogate(text.charAt(value))) {
            value++;
        }
        return Math.min(value, max);
    }

    private int approximateTokenCount(String text) {
        int count = 0;
        boolean inWord = false;
        for (int offset = 0; offset < text.length();) {
            int cp = text.codePointAt(offset);
            offset += Character.charCount(cp);
            if (Character.UnicodeScript.of(cp) == Character.UnicodeScript.HAN) {
                count++;
                inWord = false;
            } else if (Character.isLetterOrDigit(cp)) {
                if (!inWord) count++;
                inWord = true;
            } else {
                inWord = false;
            }
        }
        return count;
    }

    private String stableChunkId(long postId, int chunkIndex) {
        String key = "greenbook:post-chunk:" + postId + ":" + chunkIndex;
        return UUID.nameUUIDFromBytes(key.getBytes(StandardCharsets.UTF_8)).toString();
    }
}
