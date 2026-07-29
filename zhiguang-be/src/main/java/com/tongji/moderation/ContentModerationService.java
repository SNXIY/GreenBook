package com.tongji.moderation;

public interface ContentModerationService {
    ContentModerationResult reviewForPublish(String title, String description, String tags, String content);
}
