package com.tongji.knowpost.event;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.Instant;

/** Typed, versioned event payload shared by the projection consumers. */
public record PostLifecycleEvent(
        @JsonProperty("event_id") long eventId,
        @JsonProperty("post_id") long postId,
        @JsonProperty("event_version") long eventVersion,
        @JsonProperty("event_type") PostLifecycleEventType eventType,
        String status,
        String visibility,
        @JsonProperty("content_object_key") String contentObjectKey,
        @JsonProperty("content_etag") String contentEtag,
        @JsonProperty("content_sha256") String contentSha256,
        @JsonProperty("updated_at") Instant updatedAt,
        @JsonProperty("user_id") long userId,
        @JsonProperty("tenant_id") String tenantId
) {}
