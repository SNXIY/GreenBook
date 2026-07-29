package com.tongji.moderation.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

public record ModerationCommunityContentSnapshot(
        ModerationCommunityContentRecord current,
        ModerationCommunityContentRecord post,
        @JsonProperty("parent_comment_required") boolean parentCommentRequired
) {
}
