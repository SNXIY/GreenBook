package com.tongji.search.rag.mapper;

import com.tongji.search.rag.model.PostChunk;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

@Mapper
public interface PostChunkMapper {
    int deleteByPostId(@Param("postId") long postId);

    Long findMaxEventVersion(@Param("postId") long postId);

    int insertBatch(@Param("chunks") List<PostChunk> chunks);

    List<PostChunk> findByIds(@Param("ids") List<String> ids);

    List<PostChunk> findByPostIds(@Param("postIds") List<Long> postIds);
}
