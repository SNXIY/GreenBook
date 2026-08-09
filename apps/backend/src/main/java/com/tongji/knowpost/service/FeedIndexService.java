package com.tongji.knowpost.service;

public interface FeedIndexService {
    void indexPublishedPost(long postId);

    void recordInteraction(long userId, long postId, String metric, int delta);

    void invalidateFeedCaches();
}
