package com.tongji.search.rag;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.tongji.search.EmbeddingService;
import com.tongji.search.SearchProviderException;
import com.tongji.search.SearchProviderUnavailableException;
import com.tongji.search.config.SearchProperties;
import com.tongji.search.rag.config.RagProperties;
import com.tongji.search.rag.model.ChunkDenseSearchHit;
import com.tongji.search.rag.model.PostChunk;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/** Qdrant adapter for evidence chunks. It never touches posts_dense. */
@Service
public class QdrantChunkClient {
    private final SearchProperties searchProperties;
    private final RagProperties ragProperties;
    private final EmbeddingService embedding;
    private final ObjectMapper objectMapper;
    private final HttpClient http;

    public QdrantChunkClient(SearchProperties searchProperties,
                             RagProperties ragProperties,
                             EmbeddingService embedding,
                             ObjectMapper objectMapper) {
        this.searchProperties = searchProperties;
        this.ragProperties = ragProperties;
        this.embedding = embedding;
        this.objectMapper = objectMapper;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(searchProperties.connectTimeoutMs()))
                .build();
    }

    public List<ChunkDenseSearchHit> search(float[] vector,
                                            int limit,
                                            List<Long> candidatePostIds) {
        if (candidatePostIds == null || candidatePostIds.isEmpty()) return List.of();
        ensureCollection();
        ObjectNode body = objectMapper.createObjectNode();
        ArrayNode vectorNode = body.putArray("vector");
        for (float value : vector) vectorNode.add(value);
        body.put("limit", Math.max(1, Math.min(limit, 100)));
        body.put("with_payload", true);
        body.set("filter", postFilter(candidatePostIds));

        JsonNode root = request("POST", collectionPath("/points/search"), body);
        List<ChunkDenseSearchHit> hits = new ArrayList<>();
        for (JsonNode point : root.path("result")) {
            JsonNode payload = point.path("payload");
            String chunkId = payload.path("chunk_id").asText(point.path("id").asText(""));
            long postId = payload.path("post_id").asLong(0L);
            if (chunkId.isBlank() || postId <= 0) continue;
            hits.add(new ChunkDenseSearchHit(
                    chunkId,
                    postId,
                    point.path("score").asDouble(0.0),
                    payload.path("event_version").asLong(0L),
                    payload.path("chunk_index").asInt(0),
                    payload.path("start_offset").asInt(0),
                    payload.path("end_offset").asInt(0)
            ));
        }
        return List.copyOf(hits);
    }

    public void upsert(PostChunk chunk, float[] vector) {
        ensureCollection();
        Long currentVersion = currentVersion(chunk.getChunkId());
        if (currentVersion != null && currentVersion >= chunk.getEventVersion()) return;

        ObjectNode point = objectMapper.createObjectNode();
        point.put("id", chunk.getChunkId());
        ArrayNode values = point.putArray("vector");
        for (float value : vector) values.add(value);
        ObjectNode payload = point.putObject("payload");
        payload.put("chunk_id", chunk.getChunkId());
        payload.put("post_id", chunk.getPostId());
        payload.put("chunk_index", chunk.getChunkIndex());
        payload.put("start_offset", chunk.getStartOffset());
        payload.put("end_offset", chunk.getEndOffset());
        payload.put("event_version", chunk.getEventVersion());
        payload.put("embedding_model", chunk.getEmbeddingModel());
        payload.put("embedding_version", chunk.getEmbeddingVersion());
        payload.put("dimension", chunk.getDimension());
        payload.put("visibility", "public");
        payload.put("status", "published");

        ObjectNode body = objectMapper.createObjectNode();
        body.putArray("points").add(point);
        request("PUT", collectionPath("/points?wait=true"), body);
    }

    /** Delete all versions of a post only after the canonical event guard ran. */
    public void deleteByPostId(long postId) {
        ensureCollection();
        ObjectNode body = objectMapper.createObjectNode();
        body.set("filter", postFilter(List.of(postId)));
        request("POST", collectionPath("/points/delete?wait=true"), body);
    }

    public void ensureCollection() {
        if (!searchProperties.qdrantEnabled()) {
            throw new SearchProviderUnavailableException("Qdrant provider disabled");
        }
        if (embedding.dimension() != 384) {
            throw new SearchProviderException("RAG chunk embedding dimension must be 384, actual="
                    + embedding.dimension());
        }
        HttpResponse<String> get = send("GET", collectionPath(""), null);
        if (get.statusCode() == 200) {
            validateCollectionDimension(get);
            return;
        }
        if (get.statusCode() != 404) throw classify(get, "Qdrant chunk collection check failed");
        ObjectNode body = objectMapper.createObjectNode();
        ObjectNode vectors = body.putObject("vectors");
        vectors.put("size", embedding.dimension());
        vectors.put("distance", "Cosine");
        body.put("on_disk_payload", true);
        request("PUT", collectionPath(""), body);
    }

    private ObjectNode postFilter(List<Long> postIds) {
        ObjectNode filter = objectMapper.createObjectNode();
        ArrayNode must = filter.putArray("must");
        ObjectNode condition = must.addObject();
        condition.put("key", "post_id");
        ObjectNode match = condition.putObject("match");
        ArrayNode any = match.putArray("any");
        for (Long postId : postIds) {
            if (postId != null && postId > 0) any.add(postId);
        }
        return filter;
    }

    private void validateCollectionDimension(HttpResponse<String> response) {
        try {
            JsonNode root = objectMapper.readTree(response.body());
            JsonNode vectors = root.path("result").path("config").path("params").path("vectors");
            int actual = vectors.path("size").asInt(0);
            if (actual > 0 && actual != embedding.dimension()) {
                throw new SearchProviderException("Qdrant chunk collection dimension mismatch collection="
                        + ragProperties.qdrantCollection() + " expected=" + embedding.dimension()
                        + " actual=" + actual);
            }
        } catch (SearchProviderException e) {
            throw e;
        } catch (Exception e) {
            throw new SearchProviderException("Qdrant chunk collection metadata parse failed", e);
        }
    }

    private Long currentVersion(String chunkId) {
        HttpResponse<String> response = send("GET", collectionPath("/points/" + chunkId), null);
        if (response.statusCode() == 404) return null;
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw classify(response, "Qdrant chunk point read failed");
        }
        try {
            JsonNode root = objectMapper.readTree(response.body());
            JsonNode value = root.path("result").path("payload").path("event_version");
            return value.isNumber() ? value.asLong() : null;
        } catch (Exception e) {
            throw new SearchProviderException("Qdrant chunk point parse failed", e);
        }
    }

    private String collectionPath(String suffix) {
        return "/collections/" + ragProperties.qdrantCollection() + suffix;
    }

    private JsonNode request(String method, String path, ObjectNode body) {
        HttpResponse<String> response = send(method, path, body);
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw classify(response, "Qdrant chunk request failed: " + method + " " + path);
        }
        try {
            return response.body() == null || response.body().isBlank()
                    ? objectMapper.createObjectNode() : objectMapper.readTree(response.body());
        } catch (Exception e) {
            throw new SearchProviderException("Qdrant chunk response parse failed", e);
        }
    }

    private HttpResponse<String> send(String method, String path, ObjectNode body) {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(searchProperties.qdrantBaseUrl().replaceAll("/$", "") + path))
                    .timeout(Duration.ofMillis(searchProperties.requestTimeoutMs()))
                    .header("Content-Type", "application/json");
            HttpRequest.BodyPublisher publisher = body == null
                    ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(body));
            builder.method(method, publisher);
            return http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        } catch (Exception e) {
            throw new SearchProviderUnavailableException("Qdrant chunk provider unavailable", e);
        }
    }

    private SearchProviderException classify(HttpResponse<String> response, String message) {
        if (response.statusCode() == 408 || response.statusCode() == 429 || response.statusCode() >= 500) {
            return new SearchProviderUnavailableException(message + " status=" + response.statusCode());
        }
        return new SearchProviderException(message + " status=" + response.statusCode());
    }
}
