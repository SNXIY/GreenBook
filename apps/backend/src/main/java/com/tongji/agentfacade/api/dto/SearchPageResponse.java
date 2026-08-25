package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "Search page response")
public record SearchPageResponse(
        @Schema(description = "Search result items") java.util.List<SearchPostItem> items,
        @Schema(description = "Current page") int page,
        @Schema(description = "Page size") int size,
        @Schema(description = "Total matching rows") long total,
        @Schema(description = "Total pages") int totalPages,
        @Schema(description = "Whether another page exists") boolean hasMore,
        @Schema(description = "Sort mode") String sort,
        @Schema(description = "Actual search provider") String provider,
        @Schema(description = "Whether the response was explicitly degraded") boolean degraded
) {}
