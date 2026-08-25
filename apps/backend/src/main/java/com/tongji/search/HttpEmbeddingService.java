package com.tongji.search;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.tongji.search.config.SearchProperties;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * HTTP adapter for a real local embedding model. The endpoint is deliberately
 * small so a FastEmbed/ONNX process can serve both projection and query text
 * without changing the Qdrant or search architecture.
 */
@Service
@ConditionalOnProperty(name = "search.embedding.provider", havingValue = "multilingual-http")
public class HttpEmbeddingService implements EmbeddingService {
    private final SearchProperties properties;
    private final ObjectMapper objectMapper;
    private final HttpClient http;

    public HttpEmbeddingService(SearchProperties properties, ObjectMapper objectMapper) {
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(properties.embeddingConnectTimeoutMs()))
                .build();
    }

    @Override
    public float[] embed(String text) {
        return request(text, "query");
    }

    @Override
    public float[] embedQuery(String text) {
        return request(text, "query");
    }

    @Override
    public float[] embedDocument(String text) {
        return request(text, "document");
    }

    private float[] request(String text, String inputType) {
        try {
            ObjectNode body = objectMapper.createObjectNode();
            body.put("text", text == null ? "" : text);
            body.put("input_type", inputType);
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(properties.embeddingEndpoint()))
                    .timeout(Duration.ofMillis(properties.embeddingRequestTimeoutMs()))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(body)))
                    .build();
            HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw new SearchProviderUnavailableException("Embedding endpoint failed status="
                        + response.statusCode());
            }
            JsonNode root = objectMapper.readTree(response.body());
            JsonNode values = root.path("embedding");
            if (!values.isArray()) {
                values = root.path("data").path(0).path("embedding");
            }
            if (!values.isArray() || values.size() != properties.embeddingDimension()) {
                throw new SearchProviderException("Embedding contract dimension mismatch expected="
                        + properties.embeddingDimension() + " actual="
                        + (values.isArray() ? values.size() : -1));
            }
            float[] vector = new float[values.size()];
            double norm = 0.0;
            for (int i = 0; i < values.size(); i++) {
                vector[i] = (float) values.get(i).asDouble();
                if (!Float.isFinite(vector[i])) {
                    throw new SearchProviderException("Embedding contract returned non-finite value");
                }
                norm += vector[i] * vector[i];
            }
            norm = Math.sqrt(norm);
            if (norm == 0.0) throw new SearchProviderException("Embedding contract returned zero vector");
            for (int i = 0; i < vector.length; i++) vector[i] /= (float) norm;
            return vector;
        } catch (SearchProviderException e) {
            throw e;
        } catch (Exception e) {
            throw new SearchProviderUnavailableException("Embedding endpoint unavailable", e);
        }
    }

    @Override public int dimension() { return properties.embeddingDimension(); }
    @Override public String model() { return properties.embeddingModel(); }
    @Override public String vectorVersion() { return properties.embeddingVectorVersion(); }
}
