package com.tongji.search.rag.api;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record KnowledgeEvidenceRequest(
        @NotBlank @Size(max = 1000) String question,
        @Min(1) @Max(20) Integer topPosts,
        @Min(1) @Max(20) Integer topChunks
) {}
