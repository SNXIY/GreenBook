package com.tongji.search;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.knowpost.event.PostLifecycleEventService;
import com.tongji.knowpost.event.PostLifecycleEventType;
import com.tongji.knowpost.id.SnowflakeIdGenerator;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.relation.outbox.OutboxMapper;
import org.junit.jupiter.api.Test;

import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class PostLifecycleEventServiceTest {
    @Test
    void writesTypedVersionedPayloadToOutbox() throws Exception {
        OutboxMapper outbox = mock(OutboxMapper.class);
        ObjectMapper objectMapper = new ObjectMapper().findAndRegisterModules();
        SnowflakeIdGenerator ids = mock(SnowflakeIdGenerator.class);
        KnowPostMapper posts = mock(KnowPostMapper.class);
        KnowPost post = KnowPost.builder().id(42L).creatorId(7L).eventVersion(9L)
                .status("published").visible("public").contentObjectKey("posts/42.md")
                .contentEtag("etag").contentSha256("sha")
                .updateTime(Instant.parse("2026-08-25T00:00:00Z")).build();
        when(ids.nextId()).thenReturn(99L);
        when(posts.findById(42L)).thenReturn(post);
        when(outbox.insert(anyLong(), eq("post"), eq(42L), eq("PostPublished"), anyString())).thenReturn(1);

        PostLifecycleEventService service = new PostLifecycleEventService(outbox, objectMapper, ids, posts);
        service.emit(PostLifecycleEventType.PostPublished, post);

        var payload = org.mockito.ArgumentCaptor.forClass(String.class);
        verify(outbox).insert(eq(99L), eq("post"), eq(42L), eq("PostPublished"), payload.capture());
        JsonNode json = objectMapper.readTree(payload.getValue());
        assertEquals(99L, json.path("event_id").asLong());
        assertEquals(42L, json.path("post_id").asLong());
        assertEquals(9L, json.path("event_version").asLong());
        assertEquals("PostPublished", json.path("event_type").asText());
        assertEquals("public", json.path("visibility").asText());
    }
}
