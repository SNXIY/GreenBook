package com.tongji.agentfacade.mapper;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.Instant;

@Mapper
public interface AgentIdempotencyMapper {

    int insert(@Param("id") Long id,
               @Param("userId") Long userId,
               @Param("operation") String operation,
               @Param("idempotencyKey") String idempotencyKey,
               @Param("requestHash") String requestHash,
               @Param("status") String status,
               @Param("responseStatus") Integer responseStatus,
               @Param("responseBody") String responseBody,
               @Param("resourceType") String resourceType,
               @Param("resourceId") String resourceId,
               @Param("expiresAt") Instant expiresAt);

    AgentIdempotencyRecord findByUserOpKey(@Param("userId") Long userId,
                                            @Param("operation") String operation,
                                            @Param("idempotencyKey") String idempotencyKey);

    int complete(@Param("id") Long id,
                 @Param("responseStatus") Integer responseStatus,
                 @Param("responseBody") String responseBody,
                 @Param("resourceType") String resourceType,
                 @Param("resourceId") String resourceId,
                 @Param("status") String status);

    int deleteExpired(@Param("before") Instant before);
}
