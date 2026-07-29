package com.tongji.moderation;

import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Service;

/**
 * 对接 moderation-agent；替换原 Spring AI 内置审核。
 */
@Service
@Primary
public class AgentContentModerationService implements ContentModerationService {

    private final ModerationAgentClient client;

    public AgentContentModerationService(ModerationAgentClient client) {
        this.client = client;
    }

    @Override
    public ContentModerationResult reviewForPublish(String title, String description, String tags, String content) {
        client.submitReview(
                title, description, tags, content, null, null, null, null
        );
        return ContentModerationResult.pending("审核任务已提交");
    }
}
