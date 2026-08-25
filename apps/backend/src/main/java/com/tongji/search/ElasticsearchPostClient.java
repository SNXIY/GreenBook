package com.tongji.search;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.tongji.search.config.SearchProperties;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/** Minimal Elasticsearch HTTP adapter; it is intentionally not an Agent tool. */
@Service
public class ElasticsearchPostClient {
    private final SearchProperties properties;
    private final ObjectMapper objectMapper;
    private final HttpClient http;

    public ElasticsearchPostClient(SearchProperties properties, ObjectMapper objectMapper) {
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(properties.connectTimeoutMs()))
                .build();
    }

    public List<LexicalSearchHit> search(String query, int limit) {
        ensureIndex();
        ObjectNode body = objectMapper.createObjectNode();
        body.put("size", Math.max(1, Math.min(limit, 200)));
        ObjectNode multiMatch = body.putObject("query").putObject("multi_match");
        multiMatch.put("query", query == null ? "" : query);
        ArrayNode fields = multiMatch.putArray("fields");
        fields.add("title^4");
        fields.add("tags^2");
        fields.add("description");
        fields.add("content");
        multiMatch.put("operator", "or");

        JsonNode root = request("POST", "/" + properties.elasticsearchIndex() + "/_search", body);
        List<LexicalSearchHit> hits = new ArrayList<>();
        JsonNode values = root.path("hits").path("hits");
        int rank = 1;
        for (JsonNode hit : values) {
            String id = hit.path("_id").asText("");
            try {
                hits.add(new LexicalSearchHit(Long.parseLong(id), hit.path("_score").asDouble(0.0), rank++));
            } catch (NumberFormatException ignored) {
                // A malformed projection document is not a business result.
            }
        }
        return hits;
    }

    public void upsert(PostSearchDocument document) {
        ensureIndex();
        Long currentVersion = currentVersion(document.postId());
        if (currentVersion != null && currentVersion >= document.eventVersion()) {
            return;
        }
        ObjectNode body = objectMapper.createObjectNode();
        body.put("post_id", document.postId());
        if (document.creatorId() != null) body.put("creator_id", document.creatorId());
        put(body, "title", document.title());
        put(body, "description", document.description());
        put(body, "tags", document.tags());
        put(body, "content", document.content());
        put(body, "status", document.status());
        put(body, "visibility", document.visibility());
        if (document.publishTime() != null) body.put("publish_time", document.publishTime().toString());
        if (document.updatedAt() != null) body.put("updated_at", document.updatedAt().toString());
        body.put("event_version", document.eventVersion());
        request("PUT", "/" + properties.elasticsearchIndex() + "/_doc/" + document.postId(), body);
    }

    public void delete(long postId, long eventVersion) {
        ensureIndex();
        Long currentVersion = currentVersion(postId);
        if (currentVersion != null && currentVersion > eventVersion) {
            return;
        }
        HttpResponse<String> response = send("DELETE", "/" + properties.elasticsearchIndex() + "/_doc/" + postId, null);
        if ((response.statusCode() < 200 || response.statusCode() >= 300) && response.statusCode() != 404) {
            throw classify(response, "Elasticsearch delete failed");
        }
    }

    public void ensureIndex() {
        if (!properties.elasticsearchEnabled()) {
            throw new SearchProviderUnavailableException("Elasticsearch provider disabled");
        }
        HttpResponse<String> head = send("HEAD", "/" + properties.elasticsearchIndex(), null);
        if (head.statusCode() == 200) return;
        if (head.statusCode() != 404) throw classify(head, "Elasticsearch index check failed");

        ObjectNode body = objectMapper.createObjectNode();
        ObjectNode settings = body.putObject("settings");
        settings.put("number_of_shards", 1);
        settings.put("number_of_replicas", 0);
        ObjectNode analysis = settings.putObject("analysis").putObject("analyzer");
        ObjectNode cjk = analysis.putObject("greenbook_cjk");
        cjk.put("type", "cjk");
        cjk.put("stopwords", "_none_");
        ObjectNode mappings = body.putObject("mappings").putObject("properties");
        keyword(mappings, "post_id");
        keyword(mappings, "creator_id");
        keyword(mappings, "status");
        keyword(mappings, "visibility");
        text(mappings, "title", true);
        text(mappings, "description", false);
        text(mappings, "tags", false);
        text(mappings, "content", false);
        mappings.putObject("publish_time").put("type", "date");
        mappings.putObject("updated_at").put("type", "date");
        mappings.putObject("event_version").put("type", "long");
        request("PUT", "/" + properties.elasticsearchIndex(), body);
    }

    private Long currentVersion(long postId) {
        HttpResponse<String> response = send("GET", "/" + properties.elasticsearchIndex() + "/_doc/" + postId, null);
        if (response.statusCode() == 404) return null;
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw classify(response, "Elasticsearch document read failed");
        }
        try {
            JsonNode root = objectMapper.readTree(response.body());
            JsonNode value = root.path("_source").path("event_version");
            return value.isNumber() ? value.asLong() : null;
        } catch (Exception e) {
            throw new SearchProviderException("Elasticsearch document parse failed", e);
        }
    }

    private void keyword(ObjectNode mappings, String name) {
        mappings.putObject(name).put("type", "keyword");
    }

    private void text(ObjectNode mappings, String name, boolean withKeyword) {
        ObjectNode field = mappings.putObject(name).put("type", "text").put("analyzer", "greenbook_cjk");
        if (withKeyword) field.putObject("fields").putObject("keyword").put("type", "keyword");
    }

    private void put(ObjectNode node, String name, String value) {
        if (value == null) node.putNull(name); else node.put(name, value);
    }

    private JsonNode request(String method, String path, ObjectNode body) {
        HttpResponse<String> response = send(method, path, body);
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw classify(response, "Elasticsearch request failed: " + method + " " + path);
        }
        try {
            return response.body() == null || response.body().isBlank()
                    ? objectMapper.createObjectNode() : objectMapper.readTree(response.body());
        } catch (Exception e) {
            throw new SearchProviderException("Elasticsearch response parse failed", e);
        }
    }

    private HttpResponse<String> send(String method, String path, ObjectNode body) {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(properties.elasticsearchBaseUrl().replaceAll("/$", "") + path))
                    .timeout(Duration.ofMillis(properties.requestTimeoutMs()))
                    .header("Content-Type", "application/json");
            HttpRequest.BodyPublisher publisher = body == null
                    ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(body));
            builder.method(method, publisher);
            return http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        } catch (Exception e) {
            throw new SearchProviderUnavailableException("Elasticsearch unavailable", e);
        }
    }

    private SearchProviderException classify(HttpResponse<String> response, String message) {
        if (response.statusCode() == 408 || response.statusCode() == 429 || response.statusCode() >= 500) {
            return new SearchProviderUnavailableException(message + " status=" + response.statusCode());
        }
        return new SearchProviderException(message + " status=" + response.statusCode()
                + " body=" + truncate(response.body(), 300));
    }

    private String truncate(String value, int max) {
        if (value == null) return "";
        return value.length() <= max ? value : value.substring(0, max);
    }
}
