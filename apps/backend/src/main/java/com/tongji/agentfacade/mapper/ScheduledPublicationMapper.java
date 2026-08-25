package com.tongji.agentfacade.mapper;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.Instant;
import java.util.List;

@Mapper
public interface ScheduledPublicationMapper {

    int insert(@Param("id") Long id,
               @Param("userId") Long userId,
               @Param("draftId") Long draftId,
               @Param("runAt") Instant runAt,
               @Param("timezone") String timezone,
               @Param("status") String status,
               @Param("idempotencyKey") String idempotencyKey,
               @Param("auditActor") String auditActor);

    ScheduledPublicationRecord findById(@Param("id") Long id);

    ScheduledPublicationRecord findByUserAndIdempotencyKey(@Param("userId") Long userId,
                                                             @Param("idempotencyKey") String idempotencyKey);

    List<ScheduledPublicationRecord> findByUser(@Param("userId") Long userId);

    int countActiveByUserAndDraft(@Param("userId") Long userId,
                                  @Param("draftId") Long draftId);

    int updateRunAt(@Param("id") Long id,
                    @Param("userId") Long userId,
                    @Param("runAt") Instant runAt,
                    @Param("expectedVersion") int expectedVersion);

    int cancel(@Param("id") Long id,
               @Param("userId") Long userId,
               @Param("status") String status);

    int markPublished(@Param("id") Long id,
                      @Param("publishedPostId") Long publishedPostId,
                      @Param("publishedAt") Instant publishedAt);

    int markFailed(@Param("id") Long id,
                   @Param("failureCode") String failureCode,
                   @Param("failureMessage") String failureMessage);

    List<ScheduledPublicationRecord> findDue(@Param("before") Instant before,
                                              @Param("limit") int limit);

    int claimForExecution(@Param("id") Long id);

    List<ScheduledPublicationRecord> recoverStaleProcessing(@Param("before") Instant before,
                                                             @Param("limit") int limit);
}
