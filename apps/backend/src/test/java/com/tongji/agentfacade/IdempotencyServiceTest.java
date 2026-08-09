package com.tongji.agentfacade;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.agentfacade.mapper.AgentIdempotencyMapper;
import com.tongji.agentfacade.mapper.AgentIdempotencyRecord;
import com.tongji.agentfacade.service.IdempotencyService;
import com.tongji.common.exception.BusinessException;
import com.tongji.knowpost.id.SnowflakeIdGenerator;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class IdempotencyServiceTest {

    @Mock private AgentIdempotencyMapper mapper;
    @Mock private SnowflakeIdGenerator idGen;
    private final ObjectMapper objectMapper = new ObjectMapper();

    private IdempotencyService idempotencyService;

    @BeforeEach
    void setUp() {
        idempotencyService = new IdempotencyService(mapper, idGen, objectMapper);
    }

    @Test
    void sameKeySameHash_shouldReplayFirstResult() {
        String key = "test-key-123";
        String requestBody = "{\"title\":\"hello\"}";
        String hash = IdempotencyService.sha256(requestBody);

        AgentIdempotencyRecord existing = AgentIdempotencyRecord.builder()
                .id(1L).userId(1L).operation("CREATE_DRAFT")
                .idempotencyKey(key).requestHash(hash)
                .status("COMPLETED").responseStatus(200)
                .responseBody("{\"draftId\":\"123\"}")
                .build();

        when(mapper.findByUserOpKey(1L, "CREATE_DRAFT", key)).thenReturn(existing);

        Object result = idempotencyService.execute(1L, "CREATE_DRAFT", key, requestBody,
                Object.class,
                () -> { fail("Should not execute action on replay"); return null; });

        assertNotNull(result);
    }

    @Test
    void sameKeyDifferentHash_shouldReturnConflict() {
        String key = "test-key-456";
        String requestBody1 = "{\"title\":\"hello\"}";
        String requestBody2 = "{\"title\":\"world\"}";
        String hash1 = IdempotencyService.sha256(requestBody1);

        AgentIdempotencyRecord existing = AgentIdempotencyRecord.builder()
                .id(1L).userId(1L).operation("CREATE_DRAFT")
                .idempotencyKey(key).requestHash(hash1)
                .status("COMPLETED").responseStatus(200).build();

        when(mapper.findByUserOpKey(1L, "CREATE_DRAFT", key)).thenReturn(existing);

        assertThrows(BusinessException.class, () ->
                idempotencyService.execute(1L, "CREATE_DRAFT", key, requestBody2,
                        Object.class,
                        () -> { fail("Should not execute"); return null; }));
    }
}
