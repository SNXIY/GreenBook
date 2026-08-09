package com.tongji.auth.token;

import com.nimbusds.jose.JOSEException;
import com.nimbusds.jose.JWSAlgorithm;
import com.nimbusds.jose.JWSHeader;
import com.nimbusds.jose.crypto.RSASSASigner;
import com.nimbusds.jose.jwk.RSAKey;
import com.nimbusds.jwt.JWTClaimsSet;
import com.nimbusds.jwt.SignedJWT;
import com.tongji.auth.config.AgentJwtValidator;
import com.tongji.auth.config.AuthConfiguration;
import com.tongji.auth.config.AuthProperties;
import com.tongji.auth.config.PemUtils;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.core.io.ClassPathResource;
import org.springframework.security.oauth2.core.OAuth2TokenValidator;
import org.springframework.security.oauth2.core.OAuth2TokenValidatorResult;
import org.springframework.security.oauth2.jwt.*;

import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.interfaces.RSAPrivateKey;
import java.security.interfaces.RSAPublicKey;
import java.time.Instant;
import java.util.Date;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.*;

class JwtValidationTest {

    private static JwtDecoder decoder;
    private static JwtEncoder encoder;
    private static AuthProperties properties;
    private static RSAPrivateKey privateKey;
    private static RSAPublicKey publicKey;
    private static RSAPrivateKey wrongPrivateKey;
    private static RSAPublicKey wrongPublicKey;

    @BeforeAll
    static void setUp() throws Exception {
        properties = new AuthProperties();
        properties.getJwt().setIssuer("zhiguang-test");
        properties.getJwt().setPrivateKey(new ClassPathResource("keys/private.pem"));
        properties.getJwt().setPublicKey(new ClassPathResource("keys/public.pem"));
        AuthConfiguration config = new AuthConfiguration(properties);
        decoder = config.jwtDecoder();
        encoder = config.jwtEncoder();

        privateKey = PemUtils.readPrivateKey(properties.getJwt().getPrivateKey());
        publicKey = PemUtils.readPublicKey(properties.getJwt().getPublicKey());

        KeyPairGenerator gen = KeyPairGenerator.getInstance("RSA");
        gen.initialize(2048);
        KeyPair kp = gen.generateKeyPair();
        wrongPrivateKey = (RSAPrivateKey) kp.getPrivate();
        wrongPublicKey = (RSAPublicKey) kp.getPublic();
    }

    @Test
    void validAccessToken_shouldPass() {
        String token = createToken(privateKey, "zhiguang-test",
                List.of("zhiguang-api"), "access", Instant.now().plusSeconds(300));
        Jwt jwt = decoder.decode(token);
        assertThat(jwt).isNotNull();
        assertThat(jwt.getClaimAsString("token_type")).isEqualTo("access");
    }

    @Test
    void wrongSignature_shouldReject() {
        String token = createToken(privateKey, "zhiguang-test",
                List.of("zhiguang-api"), "access", Instant.now().plusSeconds(300));
        String tampered = token.substring(0, token.length() - 4) + "XXXX";
        assertThatThrownBy(() -> decoder.decode(tampered))
                .isInstanceOf(JwtException.class);
    }

    @Test
    void wrongIssuer_shouldReject() {
        String token = createToken(privateKey, "evil-issuer",
                List.of("zhiguang-api"), "access", Instant.now().plusSeconds(300));
        assertThatThrownBy(() -> decoder.decode(token))
                .isInstanceOf(JwtException.class);
    }

    @Test
    void audienceMissingRuntime_whenOnlyRuntimeRequired_shouldReject() {
        // Validate audience against a narrow requirement
        OAuth2TokenValidator<Jwt> narrow = new AgentJwtValidator(List.of("greenbook-assistant-runtime"));
        NimbusJwtDecoder narrowDecoder = NimbusJwtDecoder.withPublicKey(publicKey).build();
        narrowDecoder.setJwtValidator(narrow);

        String token = createToken(privateKey, "zhiguang-test",
                List.of("evil-audience"), "access", Instant.now().plusSeconds(300));

        assertThatThrownBy(() -> narrowDecoder.decode(token))
                .isInstanceOf(JwtException.class);
    }

    @Test
    void audienceContainsRuntime_shouldPass() {
        OAuth2TokenValidator<Jwt> agentValidator = new AgentJwtValidator(List.of("greenbook-assistant-runtime"));
        NimbusJwtDecoder agentDecoder = NimbusJwtDecoder.withPublicKey(publicKey).build();
        agentDecoder.setJwtValidator(agentValidator);

        String token = createToken(privateKey, "zhiguang-test",
                List.of("greenbook-assistant-runtime"), "access", Instant.now().plusSeconds(300));
        Jwt jwt = agentDecoder.decode(token);
        assertThat(jwt).isNotNull();
    }

    @Test
    void expiredAccessToken_shouldReject() {
        String token = createToken(privateKey, "zhiguang-test",
                List.of("zhiguang-api"), "access", Instant.now().minusSeconds(3600));
        assertThatThrownBy(() -> decoder.decode(token))
                .isInstanceOf(JwtException.class);
    }

    @Test
    void audienceValidator_rejectsNullAudience() {
        OAuth2TokenValidator<Jwt> v = new AgentJwtValidator(List.of("x"));
        Jwt noAud = new Jwt("token-value", Instant.now(), Instant.now().plusSeconds(300),
                Map.of("alg", "RS256"),
                Map.of("sub", "123", "token_type", "access", "iss", "zhiguang-test"));
        OAuth2TokenValidatorResult r = v.validate(noAud);
        assertThat(r.hasErrors()).isTrue();
    }

    @Test
    void audienceValidator_rejectsEmptyAudience() {
        OAuth2TokenValidator<Jwt> v = new AgentJwtValidator(List.of("x"));
        Jwt jwt = new Jwt("token-value", Instant.now(), Instant.now().plusSeconds(300),
                Map.of("alg", "RS256"),
                Map.of("sub", "123", "token_type", "access", "iss", "zhiguang-test", "aud", List.of()));
        OAuth2TokenValidatorResult r = v.validate(jwt);
        assertThat(r.hasErrors()).isTrue();
    }

    @Test
    void audienceValidator_acceptsMatchingAudience() {
        OAuth2TokenValidator<Jwt> v = new AgentJwtValidator(List.of("greenbook-assistant-runtime"));
        Jwt jwt = new Jwt("token-value", Instant.now(), Instant.now().plusSeconds(300),
                Map.of("alg", "RS256"),
                Map.of("sub", "123", "token_type", "access", "iss", "zhiguang-test", "aud", List.of("greenbook-assistant-runtime")));
        OAuth2TokenValidatorResult r = v.validate(jwt);
        assertThat(r.hasErrors()).isFalse();
    }

    // ── helpers ──────────────────────────────────────────────

    private String createToken(RSAPrivateKey key, String issuer,
                               List<String> audience, String tokenType, Instant exp) {
        try {
            RSAKey jwk = new RSAKey.Builder(publicKey).privateKey(key).keyID("test-kid").build();
            JWTClaimsSet claims = new JWTClaimsSet.Builder()
                    .issuer(issuer)
                    .audience(audience)
                    .subject("123")
                    .jwtID(UUID.randomUUID().toString())
                    .issueTime(Date.from(Instant.now()))
                    .expirationTime(Date.from(exp))
                    .claim("token_type", tokenType)
                    .claim("uid", 123L)
                    .claim("sid", "test-session")
                    .claim("role", "USER")
                    .build();
            SignedJWT signed = new SignedJWT(
                    new JWSHeader.Builder(JWSAlgorithm.RS256).keyID("test-kid").build(),
                    claims);
            signed.sign(new RSASSASigner(jwk));
            return signed.serialize();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private Jwt createDecodedToken(List<String> audience, String tokenType) {
        String token = createToken(privateKey, "zhiguang-test", audience, tokenType,
                Instant.now().plusSeconds(300));
        return decoder.decode(token);
    }
}
