package com.tongji.knowpost.event;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.tongji.knowpost.id.SnowflakeIdGenerator;
import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPost;
import com.tongji.relation.outbox.OutboxMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;

/**
 * Writes the canonical search lifecycle event into the transactional outbox.
 * The method deliberately propagates serialization/insert failures so the post
 * mutation transaction cannot commit without its projection event.
 */
@Service
public class PostLifecycleEventService {
    private final OutboxMapper outboxMapper;
    private final ObjectMapper objectMapper;
    private final SnowflakeIdGenerator idGenerator;
    private final KnowPostMapper postMapper;

    @Value("${search.lifecycle.tenant-id:zhiguang}")
    private String tenantId;

    public PostLifecycleEventService(OutboxMapper outboxMapper,
                                     ObjectMapper objectMapper,
                                     SnowflakeIdGenerator idGenerator,
                                     KnowPostMapper postMapper) {
        this.outboxMapper = outboxMapper;
        this.objectMapper = objectMapper;
        this.idGenerator = idGenerator;
        this.postMapper = postMapper;
    }

    @Transactional
    public PostLifecycleEvent emit(PostLifecycleEventType type, KnowPost post) {
        if (type == null || post == null || post.getId() == null || post.getCreatorId() == null) {
            throw new IllegalArgumentException("post lifecycle event requires type, post id and creator id");
        }
        KnowPost current = postMapper.findById(post.getId());
        if (current != null) {
            post = current;
        }
        long eventId = idGenerator.nextId();
        long eventVersion = post.getEventVersion() == null ? 0L : post.getEventVersion();
        PostLifecycleEvent event = new PostLifecycleEvent(
                eventId,
                post.getId(),
                eventVersion,
                type,
                post.getStatus(),
                post.getVisible(),
                post.getContentObjectKey(),
                post.getContentEtag(),
                post.getContentSha256(),
                post.getUpdateTime() != null ? post.getUpdateTime() : Instant.now(),
                post.getCreatorId(),
                tenantId
        );
        final String payload;
        try {
            payload = objectMapper.writeValueAsString(event);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("post lifecycle event serialization failed", e);
        }
        int inserted = outboxMapper.insert(
                eventId,
                "post",
                post.getId(),
                type.name(),
                payload
        );
        if (inserted != 1) {
            throw new IllegalStateException("post lifecycle outbox insert affected " + inserted + " rows");
        }
        return event;
    }
}
