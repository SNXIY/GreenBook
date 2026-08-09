package com.tongji.common.trace;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.UUID;
import java.util.regex.Pattern;

@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class TraceContextFilter extends OncePerRequestFilter {

    public static final String HEADER_TRACE_ID = "X-Trace-ID";
    public static final String HEADER_CONVERSATION_ID = "X-Conversation-Id";
    public static final String HEADER_AGENT_RUN_ID = "X-Agent-Run-Id";
    public static final String HEADER_TOOL_CALL_ID = "X-Tool-Call-Id";

    public static final String MDC_TRACE_ID = "traceId";
    public static final String MDC_CONVERSATION_ID = "conversationId";
    public static final String MDC_AGENT_RUN_ID = "agentRunId";

    private static final Pattern SAFE_HEADER =
            Pattern.compile("^[A-Za-z0-9._:\\-/]{1,128}$");

    public static String currentOrCreate() {
        String current = MDC.get(MDC_TRACE_ID);
        return current == null || current.isBlank()
                ? UUID.randomUUID().toString()
                : current;
    }

    public static String conversationId() {
        return MDC.get(MDC_CONVERSATION_ID);
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain
    ) throws ServletException, IOException {
        String traceId = safeOrCreate(request.getHeader(HEADER_TRACE_ID));
        MDC.put(MDC_TRACE_ID, traceId);
        response.setHeader(HEADER_TRACE_ID, traceId);

        String conversationId = safe(request.getHeader(HEADER_CONVERSATION_ID));
        if (conversationId != null) {
            MDC.put(MDC_CONVERSATION_ID, conversationId);
        }

        String agentRunId = safe(request.getHeader(HEADER_AGENT_RUN_ID));
        if (agentRunId != null) {
            MDC.put(MDC_AGENT_RUN_ID, agentRunId);
        }

        try {
            filterChain.doFilter(request, response);
        } finally {
            MDC.remove(MDC_TRACE_ID);
            MDC.remove(MDC_CONVERSATION_ID);
            MDC.remove(MDC_AGENT_RUN_ID);
        }
    }

    private String safeOrCreate(String value) {
        if (value != null && SAFE_HEADER.matcher(value).matches()) {
            return value;
        }
        return UUID.randomUUID().toString();
    }

    private String safe(String value) {
        if (value != null && SAFE_HEADER.matcher(value).matches()) {
            return value;
        }
        return null;
    }
}
