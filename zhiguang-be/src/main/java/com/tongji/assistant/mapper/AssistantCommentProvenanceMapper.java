package com.tongji.assistant.mapper;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

@Mapper
public interface AssistantCommentProvenanceMapper {
    @Select("""
            SELECT comment_id
            FROM assistant_comment_provenance
            WHERE assistant_run_id = #{assistantRunId}
            LIMIT 1
            """)
    Long findCommentIdByRunId(@Param("assistantRunId") String assistantRunId);

    @Insert("""
            INSERT INTO assistant_comment_provenance (
                comment_id, assistant_run_id, source_post_id, source_post_sha256, created_at
            ) VALUES (
                #{commentId}, #{assistantRunId}, #{sourcePostId}, #{sourcePostSha256}, NOW(3)
            )
            """)
    int insert(
            @Param("commentId") Long commentId,
            @Param("assistantRunId") String assistantRunId,
            @Param("sourcePostId") Long sourcePostId,
            @Param("sourcePostSha256") String sourcePostSha256
    );
}
