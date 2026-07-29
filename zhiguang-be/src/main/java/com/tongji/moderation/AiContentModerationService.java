package com.tongji.moderation;

import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import org.springframework.ai.chat.client.ChatClient;

import java.util.List;
import java.util.Locale;

/**
 * @deprecated 已由 {@link AgentContentModerationService} 替换，保留仅作参考。
 */
@Deprecated
public class AiContentModerationService implements ContentModerationService {
    private static final int MAX_CONTENT_CHARS = 6000;

    private final SensitiveWordService sensitiveWordService;
    private final ChatClient chatClient;

    public AiContentModerationService(SensitiveWordService sensitiveWordService, ChatClient chatClient) {
        this.sensitiveWordService = sensitiveWordService;
        this.chatClient = chatClient;
    }

    @Override
    public ContentModerationResult reviewForPublish(String title, String description, String tags, String content) {
        List<String> hitWords = sensitiveWordService.findAll(title, description, tags, content);
        if (hitWords.isEmpty()) {
            return ContentModerationResult.pass();
        }

        String modelResult = callSemanticReview(title, description, tags, content, hitWords);
        String normalized = modelResult == null ? "" : modelResult.trim().toUpperCase(Locale.ROOT);
        if (normalized.startsWith("PASS")) {
            return ContentModerationResult.pass();
        }
        if (normalized.startsWith("REJECT")) {
            String reason = modelResult.length() > 6 ? modelResult.substring(6).trim() : "内容存在违规风险";
            return ContentModerationResult.reject(hitWords, reason.isBlank() ? "内容存在违规风险" : reason);
        }

        throw new BusinessException(ErrorCode.BAD_REQUEST, "内容命中风险词，AI审核结果异常，请稍后重试");
    }

    private String callSemanticReview(String title, String description, String tags, String content, List<String> hitWords) {
        String system = """
                你是内容安全审核员。请判断用户发布内容是否违规。
                规则：
                1. 如果内容是反诈、科普、新闻讨论、劝阻危害等正常语境，即使命中风险词，也输出 PASS。
                2. 如果内容包含违法交易、赌博/博彩引流、色情低俗、暴恐、诈骗、联系方式导流等违规意图，输出 REJECT，并给出简短原因。
                3. 只输出一行，格式必须是：PASS 或 REJECT 原因。
                """;
        String user = """
                命中风险词：%s
                标题：%s
                描述：%s
                标签：%s
                正文：
                %s
                """.formatted(
                String.join("、", hitWords),
                nullToEmpty(title),
                nullToEmpty(description),
                nullToEmpty(tags),
                truncate(nullToEmpty(content), MAX_CONTENT_CHARS)
        );

        try {
            return chatClient.prompt()
                    .system(system)
                    .user(user)
                    .call()
                    .content();
        } catch (Exception ex) {
            throw new BusinessException(ErrorCode.BAD_REQUEST, "内容命中风险词，AI审核暂不可用，请稍后重试");
        }
    }

    private String nullToEmpty(String text) {
        return text == null ? "" : text;
    }

    private String truncate(String text, int maxChars) {
        if (text.length() <= maxChars) {
            return text;
        }
        return text.substring(0, maxChars);
    }
}
