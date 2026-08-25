package com.tongji.search;

import com.tongji.search.config.SearchProperties;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Locale;

/**
 * Deterministic local post-level vector baseline. It keeps projection/rebuild
 * reproducible without adding a second service; semantic quality is evaluated
 * separately and can later be replaced behind EmbeddingService.
 */
@Service
public class HashingEmbeddingService implements EmbeddingService {
    private final SearchProperties properties;

    public HashingEmbeddingService(SearchProperties properties) {
        this.properties = properties;
    }

    @Override
    public float[] embed(String text) {
        if (!"hashing".equalsIgnoreCase(properties.embeddingProvider())) {
            throw new SearchProviderUnavailableException("unsupported embedding provider: "
                    + properties.embeddingProvider());
        }
        int dimension = properties.embeddingDimension();
        float[] vector = new float[dimension];
        String normalized = text == null ? "" : text.toLowerCase(Locale.ROOT).trim();
        if (normalized.isEmpty()) {
            return vector;
        }
        String[] terms = normalized.split("[^\\p{L}\\p{N}]+|(?<=[\\p{IsHan}])|(?=[\\p{IsHan}])");
        int termIndex = 0;
        for (String term : terms) {
            if (term.isBlank()) continue;
            byte[] digest = digest(term + "#" + termIndex++);
            for (int i = 0; i < dimension; i++) {
                int b = digest[i % digest.length] & 0xff;
                vector[i] += ((b / 255.0f) * 2.0f - 1.0f) / Math.max(1, terms.length);
            }
        }
        double norm = 0.0;
        for (float value : vector) norm += value * value;
        norm = Math.sqrt(norm);
        if (norm > 0) {
            for (int i = 0; i < vector.length; i++) vector[i] /= (float) norm;
        }
        return vector;
    }

    private byte[] digest(String value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException("SHA-256 is required for the local embedding baseline", e);
        }
    }

    @Override public int dimension() { return properties.embeddingDimension(); }
    @Override public String model() { return properties.embeddingModel(); }
    @Override public String vectorVersion() { return properties.embeddingVectorVersion(); }
}
