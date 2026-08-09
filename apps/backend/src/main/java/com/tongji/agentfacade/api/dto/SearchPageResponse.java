package com.tongji.agentfacade.api.dto;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "搜索分页响应")
public record SearchPageResponse(
        @Schema(description = "搜索结果列表") java.util.List<SearchPostItem> items,
        @Schema(description = "当前页码") int page,
        @Schema(description = "每页大小") int size,
        @Schema(description = "总条数") long total,
        @Schema(description = "总页数") int totalPages,
        @Schema(description = "是否有下一页") boolean hasMore,
        @Schema(description = "排序方式") String sort
) {}
