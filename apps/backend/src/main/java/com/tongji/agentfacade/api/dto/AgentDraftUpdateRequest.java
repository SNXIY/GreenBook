package com.tongji.agentfacade.api.dto;

import com.tongji.agentfacade.contract.DraftMetadataContract;
import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.Size;

import java.time.Instant;

@Schema(description = "Draft update request")
public record AgentDraftUpdateRequest(
        @Schema(description = "Title") String title,
        @Schema(description = "Markdown body") String content,
        @Schema(description = "Short summary stored in know_posts.description")
        @Size(max = DraftMetadataContract.DESCRIPTION_MAX_LENGTH) String summary,
        @Schema(description = "Tags") java.util.List<String> tags,
        @Schema(description = "Visibility") String visibility,
        @Schema(description = "Expected draft version for optimistic locking")
        Instant expectedVersion
) {}
