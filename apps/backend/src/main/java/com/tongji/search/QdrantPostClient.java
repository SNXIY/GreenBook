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

/** Post-level Qdrant adapter. Chunk collections are intentionally out of scope. */
@Service
public class QdrantPostClient {
    private final SearchProperties properties;
    private final ObjectMapper objectMapper;
    private final HttpClient http;

    public QdrantPostClient(SearchProperties properties, ObjectMapper objectMapper) {
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(properties.connectTimeoutMs()))
                .build();
    }

    public List<DenseSearchHit> search(float[] vector, int limit) {
        ensureCollection();
        ObjectNode body = objectMapper.createObjectNode();
        ArrayNode vectorNode = body.putArray("vector");
        for (float value : vector) vectorNode.add(value);
        body.put("limit", Math.max(1, Math.min(limit, 200)));
        body.put("with_payload", true);
        JsonNode root = request("POST", collectionPath("/points/search"), body);
        List<DenseSearchHit> hits = new ArrayList<>();
        int rank = 1;
        for (JsonNode point : root.path("result")) {
            JsonNode payload = point.path("payload");
            long postId = payload.path("post_id").asLong(point.path("id").asLong(0));
            if (postId > 0) hits.add(new DenseSearchHit(postId, point.path("score").asDouble(0.0), rank++));
        }
        return hits;
    }

    public void upsert(PostSearchDocument document, float[] vector, EmbeddingService embeddingService) {
        ensureCollection();
        Long currentVersion = currentVersion(document.postId());
        if (currentVersion != null && currentVersion >= document.eventVersion()) return;

        ObjectNode point = objectMapper.createObjectNode();
        point.put("id", document.postId());
        ArrayNode values = point.putArray("vector");
        for (float value : vector) values.add(value);
        ObjectNode payload = point.putObject("payload");
        payload.put("post_id", document.postId());
        payload.put("event_version", document.eventVersion());
        payload.put("vector_version", embeddingService.vectorVersion());
        put(payload, "status", document.status());
        put(payload, "visibility", document.visibility());
        if (document.updatedAt() != null) payload.put("updated_at", document.updatedAt().toString());

        ObjectNode body = objectMapper.createObjectNode();
        body.putArray("points").add(point);
        request("PUT", collectionPath("/points?wait=true"), body);
    }

    public void delete(long postId, long eventVersion) {
        ensureCollection();
        Long currentVersion = currentVersion(postId);
        if (currentVersion != null && currentVersion > eventVersion) return;
        ObjectNode body = objectMapper.createObjectNode();
        body.putArray("points").add(postId);
        request("POST", collectionPath("/points/delete?wait=true"), body);
    }

    public void ensureCollection() {
        if (!properties.qdrantEnabled()) {
            throw new SearchProviderUnavailableException("Qdrant provider disabled");
        }
        HttpResponse<String> get = send("GET", collectionPath(""), null);
        if (get.statusCode() == 200) return;
        if (get.statusCode() != 404) throw classify(get, "Qdrant collection check failed");
        ObjectNode body = objectMapper.createObjectNode();
        ObjectNode vectors = body.putObject("vectors");
        vectors.put("size", properties.embeddingDimension());
        vectors.put("distance", "Cosine");
        body.put("on_disk_payload", true);
        request("PUT", collectionPath(""), body);
    }

    private Long currentVersion(long postId) {
        HttpResponse<String> response = send("GET", collectionPath("/points/" + postId), null);
        if (response.statusCode() == 404) return null;
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw classify(response, "Qdrant point read failed");
        }
        try {
            JsonNode root = objectMapper.readTree(response.body());
            JsonNode value = root.path("result").path("payload").path("event_version");
            return value.isNumber() ? value.asLong() : null;
        } catch (Exception e) {
            throw new SearchProviderException("Qdrant point parse failed", e);
        }
    }

    private String collectionPath(String suffix) {
        return "/collections/" + properties.qdrantCollection() + suffix;
    }

    private void put(ObjectNode node, String name, String value) {
        if (value == null) node.putNull(name); else node.put(name, value);
    }

    private JsonNode request(String method, String path, ObjectNode body) {
        HttpResponse<String> response = send(method, path, body);
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw classify(response, "Qdrant request failed: " + method + " " + path);
        }
        try {
            return response.body() == null || response.body().isBlank()
                    ? objectMapper.createObjectNode() : objectMapper.readTree(response.body());
        } catch (Exception e) {
            throw new SearchProviderException("Qdrant response parse failed", e);
        }
    }

    private HttpResponse<String> send(String method, String path, ObjectNode body) {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(properties.qdrantBaseUrl().replaceAll("/$", "") + path))
                    .timeout(Duration.ofMillis(properties.requestTimeoutMs()))
                    .header("Content-Type", "application/json");
            HttpRequest.BodyPublisher publisher = body == null
                    ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(body));
            builder.method(method, publisher);
            return http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        } catch (Exception e) {
            throw new SearchProviderUnavailableException("Qdrant unavailable", e);
        }
    }

    private SearchProviderException classify(HttpResponse<String> response, String message) {
        if (response.statusCode() == 408 || response.statusCode() == 429 || response.statusCode() >= 500) {
            return new SearchProviderUnavailableException(message + " status=" + response.statusCode());
        }
        return new SearchProviderException(message + " status=" + response.statusCode());
    }
}
