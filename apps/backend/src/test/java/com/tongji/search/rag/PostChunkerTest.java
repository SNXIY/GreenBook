package com.tongji.search.rag;

import com.tongji.search.rag.model.PostChunk;
import com.tongji.search.rag.service.PostChunker;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

class PostChunkerTest {
    @Test
    void splitsMixedLanguageParagraphsWithStableOffsetsAndIds() {
        String content = "第一段介绍 Java backend。\n\nSecond paragraph explains semantic retrieval and overlap.";
        PostChunker chunker = new PostChunker(64, 12);

        List<PostChunk> first = chunker.chunk(42L, 3L, content, "model", "v1", 384);
        List<PostChunk> second = chunker.chunk(42L, 4L, content, "model", "v1", 384);

        assertTrue(first.size() >= 2);
        assertEquals(first.size(), second.size());
        for (int index = 0; index < first.size(); index++) {
            PostChunk left = first.get(index);
            PostChunk right = second.get(index);
            assertEquals(left.getChunkId(), right.getChunkId());
            assertEquals(left.getContent(), content.substring(left.getStartOffset(), left.getEndOffset()));
            assertTrue(left.getTokenCount() > 0);
            assertEquals(384, left.getDimension());
        }
    }

    @Test
    void longParagraphUsesOverlapWithoutInfiniteLoop() {
        String content = "中文 English ".repeat(80);
        List<PostChunk> chunks = new PostChunker(80, 20)
                .chunk(7L, 1L, content, "model", "v1", 384);

        assertTrue(chunks.size() > 5);
        for (int index = 1; index < chunks.size(); index++) {
            assertTrue(chunks.get(index).getStartOffset() > chunks.get(index - 1).getStartOffset());
        }
    }
}
