package com.tongji.assistant.mapper;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Update;

import java.time.Instant;

@Mapper
public interface AssistantCapabilityMapper {
    @Insert("""
            INSERT INTO assistant_capabilities (
                id, run_id, user_id, actions_json, resources_json,
                max_uses, use_count, expires_at, revoked, created_at
            ) VALUES (
                #{id}, #{runId}, #{userId}, #{actionsJson}, #{resourcesJson},
                #{maxUses}, 0, #{expiresAt}, 0, NOW(3)
            )
            """)
    int insert(
            @Param("id") String id,
            @Param("runId") String runId,
            @Param("userId") Long userId,
            @Param("actionsJson") String actionsJson,
            @Param("resourcesJson") String resourcesJson,
            @Param("maxUses") Integer maxUses,
            @Param("expiresAt") Instant expiresAt
    );

    @Update("""
            UPDATE assistant_capabilities
            SET use_count = use_count + 1,
                last_used_at = NOW(3)
            WHERE id = #{id}
              AND revoked = 0
              AND expires_at > NOW(3)
              AND use_count < max_uses
            """)
    int consume(@Param("id") String id);

    @Update("""
            UPDATE assistant_capabilities
            SET revoked = 1
            WHERE id = #{id}
              AND user_id = #{userId}
              AND revoked = 0
            """)
    int revoke(
            @Param("id") String id,
            @Param("userId") Long userId
    );
}
