package com.tongji.comment.mapper;

import com.tongji.comment.model.Comment;
import com.tongji.comment.model.CommentRow;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

@Mapper
public interface CommentMapper {
    int insert(Comment comment);

    CommentRow findById(@Param("id") Long id);

    Long findPostCreatorId(@Param("postId") Long postId);

    int incrementReplyCount(@Param("id") Long id);

    int decrementReplyCount(@Param("id") Long id);

    int softDelete(@Param("id") Long id, @Param("userId") Long userId);

    int updateTop(@Param("id") Long id, @Param("postCreatorId") Long postCreatorId, @Param("top") boolean top);

    List<CommentRow> listTopLevel(@Param("postId") Long postId,
                                  @Param("cursor") Long cursor,
                                  @Param("size") int size);

    List<CommentRow> listReplies(@Param("parentId") Long parentId,
                                 @Param("cursor") Long cursor,
                                 @Param("size") int size);

    List<CommentRow> listByIds(@Param("ids") List<Long> ids);

}
