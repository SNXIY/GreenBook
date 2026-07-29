package com.tongji.comment.model;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.Instant;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class Comment {
    private Long id;
    private Long postId;
    private Long parentId;
    private Long rootId;
    private Long userId;
    private String content;
    private String status;
    private Boolean isTop;
    private Integer replyCount;
    private Instant createTime;
    private Instant updateTime;
}
