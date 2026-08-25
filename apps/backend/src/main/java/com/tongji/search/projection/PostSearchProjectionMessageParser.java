package com.tongji.search.projection;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.knowpost.event.PostLifecycleEvent;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;

@Component
public class PostSearchProjectionMessageParser {
    private final ObjectMapper objectMapper;

    public PostSearchProjectionMessageParser(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    public List<PostLifecycleEvent> parse(String message) {
        try {
            JsonNode root = objectMapper.readTree(message);
            List<PostLifecycleEvent> events = new ArrayList<>();
            JsonNode data = root.has("data") ? root.path("data") : root;
            if (data.isArray()) {
                for (JsonNode row : data) {
                    JsonNode payload = row.path("payload");
                    if (payload.isTextual()) payload = objectMapper.readTree(payload.asText());
                    events.add(objectMapper.treeToValue(payload, PostLifecycleEvent.class));
                }
            } else {
                events.add(objectMapper.treeToValue(data, PostLifecycleEvent.class));
            }
            for (PostLifecycleEvent event : events) {
                if (event == null || event.postId() <= 0 || event.eventVersion() < 0) {
                    throw new IllegalArgumentException("invalid post search lifecycle event");
                }
            }
            return events;
        } catch (Exception e) {
            throw new IllegalArgumentException("invalid post search projection message", e);
        }
    }
}
