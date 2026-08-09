package com.tongji.config;

import io.swagger.v3.oas.annotations.OpenAPIDefinition;
import io.swagger.v3.oas.annotations.enums.SecuritySchemeType;
import io.swagger.v3.oas.annotations.info.Contact;
import io.swagger.v3.oas.annotations.info.Info;
import io.swagger.v3.oas.annotations.security.SecurityScheme;
import io.swagger.v3.oas.annotations.servers.Server;
import org.springframework.context.annotation.Configuration;

@Configuration
@OpenAPIDefinition(
        info = @Info(
                title = "GreenBook Community API",
                version = "1.0.0",
                description = "GreenBook 社区后端 API，包含 Agent Facade 稳定接口",
                contact = @Contact(name = "GreenBook Team")
        ),
        servers = {
                @Server(url = "http://127.0.0.1:8080", description = "本地开发")
        }
)
@SecurityScheme(
        name = "bearerAuth",
        type = SecuritySchemeType.HTTP,
        scheme = "bearer",
        bearerFormat = "JWT",
        description = "RS256 JWT Access Token"
)
public class OpenApiConfig {
}
