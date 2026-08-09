package com.tongji.auth.api;

import com.nimbusds.jose.jwk.JWKSet;
import com.nimbusds.jose.jwk.RSAKey;
import com.tongji.auth.config.AuthProperties;
import com.tongji.auth.config.PemUtils;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.security.interfaces.RSAPublicKey;
import java.util.Map;

/**
 * Public signing keys used by trusted internal services to verify Zhiguang JWTs.
 * Only the public key is exposed.
 */
@RestController
@RequiredArgsConstructor
public class JwksController {

    private final AuthProperties properties;

    @GetMapping("/.well-known/jwks.json")
    public Map<String, Object> jwks() {
        RSAPublicKey publicKey = PemUtils.readPublicKey(properties.getJwt().getPublicKey());
        RSAKey key = new RSAKey.Builder(publicKey)
                .keyID(properties.getJwt().getKeyId())
                .build();
        return new JWKSet(key).toJSONObject();
    }
}
