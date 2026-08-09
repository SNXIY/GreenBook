package com.tongji.knowpost.listener;

import com.tongji.knowpost.mapper.KnowPostMapper;
import com.tongji.knowpost.model.KnowPostFeedRow;
import com.tongji.knowpost.service.FeedIndexService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

import java.util.List;

@Slf4j
@Component
@RequiredArgsConstructor
public class FeedPoolWarmup {
    private final KnowPostMapper knowPostMapper;
    private final FeedIndexService feedIndexService;

    @EventListener(ApplicationReadyEvent.class)
    public void warmupRecallPools() {
        try {
            List<KnowPostFeedRow> rows = knowPostMapper.listFeedPublic(500, 0);
            for (KnowPostFeedRow row : rows) {
                feedIndexService.indexPublishedPost(row.getId());
            }
            log.info("Feed recall pools warmed up, size={}", rows.size());
        } catch (Exception ex) {
            log.warn("Feed recall pool warmup failed: {}", ex.getMessage());
        }
    }
}
