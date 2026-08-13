package com.tongji.comment.model;

import lombok.Data;

import java.time.Instant;

@Data
public class CommentRow {
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
    private String authorNickname;
    private String authorAvatar;
    private Boolean assistant;
}
