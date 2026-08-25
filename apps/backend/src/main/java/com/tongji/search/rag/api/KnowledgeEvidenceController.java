package com.tongji.search.rag.api;

import com.tongji.search.rag.EvidenceRetrievalService;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.security.SecurityRequirement;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Domain-level Agent facade endpoint; raw ES/Qdrant/chunk search is not exposed. */
@RestController
@RequestMapping("/api/v1/agent/community/knowledge")
@RequiredArgsConstructor
@Tag(name = "Community Knowledge", description = "Grounded community evidence capability")
@SecurityRequirement(name = "bearerAuth")
public class KnowledgeEvidenceController {
    private final EvidenceRetrievalService evidenceRetrieval;

    @PostMapping("/evidence")
    @Operation(summary = "Retrieve evidence for grounded community knowledge answers")
    public KnowledgeEvidenceResponse retrieve(@Valid @RequestBody KnowledgeEvidenceRequest request) {
        return evidenceRetrieval.retrieve(request.question(), request.topPosts(), request.topChunks());
    }
}
