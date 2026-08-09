package com.tongji.assistant;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.tongji.assistant.api.dto.AssistantCapabilityRequest;
import com.tongji.assistant.mapper.AssistantCapabilityMapper;
import com.tongji.assistant.service.AssistantCapabilityService;
import com.tongji.auth.config.AuthConfiguration;
import com.tongji.auth.config.AuthProperties;
import com.tongji.auth.token.JwtService;
import com.tongji.common.exception.BusinessException;
import com.tongji.user.domain.User;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.core.io.ClassPathResource;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtEncoder;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class AssistantCapabilityServiceTest {
    private AssistantCapabilityMapper mapper;
    private JwtService jwtService;
    private AssistantCapabilityService service;

    @BeforeEach
    void setUp() {
        AuthProperties properties = new AuthProperties();
        properties.getJwt().setIssuer("test-issuer");
        properties.getJwt().setPrivateKey(new ClassPathResource("keys/private.pem"));
        properties.getJwt().setPublicKey(new ClassPathResource("keys/public.pem"));
        AuthConfiguration configuration = new AuthConfiguration(properties);
        JwtEncoder encoder = configuration.jwtEncoder();
        JwtDecoder decoder = configuration.jwtDecoder();
        jwtService = new JwtService(encoder, decoder, properties);
        mapper = mock(AssistantCapabilityMapper.class);
        service = new AssistantCapabilityService(
                encoder,
                jwtService,
                properties,
                mapper,
                new ObjectMapper()
        );
    }

    @Test
    void issuesAndConsumesResourceScopedCapability() {
        String accessToken = jwtService.issueTokenPair(
                User.builder().id(7L).nickname("tester").build()
        ).accessToken();
        var issued = service.issue(
                accessToken,
                new AssistantCapabilityRequest(
                        "run-1",
                        List.of("publication.publish_now"),
                        List.of("post:42"),
                        120,
                        1
                )
        );
        when(mapper.consume(issued.capabilityId())).thenReturn(1);

        var principal = service.authorize(
                issued.token(),
                "publication.publish_now",
                List.of("post:42")
        );

        assertThat(principal.userId()).isEqualTo(7L);
        assertThat(principal.runId()).isEqualTo("run-1");
        assertThat(principal.capabilityId()).isEqualTo(issued.capabilityId());
        verify(mapper).insert(
                anyString(),
                anyString(),
                any(),
                anyString(),
                anyString(),
                any(),
                any()
        );
    }

    @Test
    void rejectsAResourceOutsideCapabilityWithoutConsumingIt() {
        String accessToken = jwtService.issueTokenPair(
                User.builder().id(7L).nickname("tester").build()
        ).accessToken();
        var issued = service.issue(
                accessToken,
                new AssistantCapabilityRequest(
                        "run-1",
                        List.of("community.get_post"),
                        List.of("post:42"),
                        120,
                        1
                )
        );

        assertThrows(
                BusinessException.class,
                () -> service.authorize(
                        issued.token(),
                        "community.get_post",
                        List.of("post:43")
                )
        );
        verify(mapper, never()).consume(anyString());
    }

    @Test
    void userCanIdempotentlyRevokeOwnCapability() {
        String accessToken = jwtService.issueTokenPair(
                User.builder().id(7L).nickname("tester").build()
        ).accessToken();
        String capabilityId = "5fd27db2-0540-4c34-84be-9bcfbde5ea06";

        service.revoke(accessToken, capabilityId);

        verify(mapper).revoke(capabilityId, 7L);
    }
}
