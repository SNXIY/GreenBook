package com.tongji.notification.mapper;

import com.tongji.notification.model.Notification;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.Instant;
import java.util.List;

@Mapper
public interface NotificationMapper {
    int insertDedup(@Param("eventId") String eventId,
                    @Param("receiverId") Long receiverId,
                    @Param("createTime") Instant createTime);

    int insert(Notification notification);

    Notification findRecentAggregate(@Param("receiverId") Long receiverId,
                                     @Param("type") String type,
                                     @Param("targetType") String targetType,
                                     @Param("targetId") String targetId,
                                     @Param("since") Instant since);

    int updateAggregate(@Param("id") Long id,
                        @Param("eventId") String eventId,
                        @Param("latestActorId") Long latestActorId,
                        @Param("content") String content,
                        @Param("extraJson") String extraJson,
                        @Param("updateTime") Instant updateTime);

    List<Notification> listByReceiver(@Param("receiverId") Long receiverId,
                                      @Param("cursor") Long cursor,
                                      @Param("limit") int limit);

    long countUnread(@Param("receiverId") Long receiverId);

    int markReadBatch(@Param("receiverId") Long receiverId,
                      @Param("ids") List<Long> ids,
                      @Param("readTime") Instant readTime);

    int markAllRead(@Param("receiverId") Long receiverId,
                    @Param("readTime") Instant readTime);
}
