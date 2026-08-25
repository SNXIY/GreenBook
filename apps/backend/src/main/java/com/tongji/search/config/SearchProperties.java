package com.tongji.search.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/** Explicit runtime configuration for search providers and projections. */
@Component
public class SearchProperties {
    @Value("${search.hybrid.enabled:true}")
    private boolean hybridEnabled;
    @Value("${search.elasticsearch.enabled:true}")
    private boolean elasticsearchEnabled;
    @Value("${search.elasticsearch.base-url:http://127.0.0.1:29200}")
    private String elasticsearchBaseUrl;
    @Value("${search.elasticsearch.index:greenbook_posts_v1}")
    private String elasticsearchIndex;
    @Value("${search.qdrant.enabled:true}")
    private boolean qdrantEnabled;
    @Value("${search.qdrant.base-url:http://127.0.0.1:26333}")
    private String qdrantBaseUrl;
    @Value("${search.qdrant.collection:posts_dense}")
    private String qdrantCollection;
    @Value("${search.embedding.provider:hashing}")
    private String embeddingProvider;
    @Value("${search.embedding.model:greenbook-post-hashing-256-v1}")
    private String embeddingModel;
    @Value("${search.embedding.vector-version:posts-dense-v1}")
    private String embeddingVectorVersion;
    @Value("${search.embedding.dimension:256}")
    private int embeddingDimension;
    @Value("${search.embedding.endpoint:http://127.0.0.1:8181/embed}")
    private String embeddingEndpoint;
    @Value("${search.embedding.connect-timeout-ms:1000}")
    private int embeddingConnectTimeoutMs;
    @Value("${search.embedding.request-timeout-ms:5000}")
    private int embeddingRequestTimeoutMs;
    @Value("${search.provider.connect-timeout-ms:800}")
    private int connectTimeoutMs;
    @Value("${search.provider.request-timeout-ms:2500}")
    private int requestTimeoutMs;
    @Value("${search.hybrid.candidate-limit:100}")
    private int candidateLimit;
    @Value("${search.hybrid.bm25-top-n:50}")
    private int bm25TopN;
    @Value("${search.hybrid.dense-top-n:100}")
    private int denseTopN;
    @Value("${search.hybrid.rrf-k:100}")
    private int rrfK;
    @Value("${search.hybrid.business-rerank.enabled:false}")
    private boolean businessRerankEnabled;
    @Value("${search.mysql.max-hot-candidates:10000}")
    private int maxHotCandidates;

    public boolean hybridEnabled() { return hybridEnabled; }
    public boolean elasticsearchEnabled() { return elasticsearchEnabled; }
    public String elasticsearchBaseUrl() { return elasticsearchBaseUrl; }
    public String elasticsearchIndex() { return elasticsearchIndex; }
    public boolean qdrantEnabled() { return qdrantEnabled; }
    public String qdrantBaseUrl() { return qdrantBaseUrl; }
    public String qdrantCollection() { return qdrantCollection; }
    public String embeddingProvider() { return embeddingProvider; }
    public String embeddingModel() { return embeddingModel; }
    public String embeddingVectorVersion() { return embeddingVectorVersion; }
    public int embeddingDimension() { return embeddingDimension; }
    public String embeddingEndpoint() { return embeddingEndpoint; }
    public int embeddingConnectTimeoutMs() { return embeddingConnectTimeoutMs; }
    public int embeddingRequestTimeoutMs() { return embeddingRequestTimeoutMs; }
    public int connectTimeoutMs() { return connectTimeoutMs; }
    public int requestTimeoutMs() { return requestTimeoutMs; }
    public int candidateLimit() { return candidateLimit; }
    public int bm25TopN() { return bm25TopN; }
    public int denseTopN() { return denseTopN; }
    public int rrfK() { return rrfK; }
    public boolean businessRerankEnabled() { return businessRerankEnabled; }
    public int maxHotCandidates() { return maxHotCandidates; }
}
