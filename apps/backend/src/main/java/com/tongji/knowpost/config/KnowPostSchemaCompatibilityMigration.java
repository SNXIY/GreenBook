package com.tongji.knowpost.config;

import lombok.RequiredArgsConstructor;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * Small compatibility bridge for databases created before AI provenance was added.
 *
 * New installations use db/schema.sql. This runner keeps existing developer
 * databases usable until the project adopts versioned Flyway migrations.
 */
@Component
@RequiredArgsConstructor
public class KnowPostSchemaCompatibilityMigration implements ApplicationRunner {

    private final JdbcTemplate jdbcTemplate;

    @Override
    public void run(ApplicationArguments args) {
        addColumnIfMissing(
                "content_origin",
                "ALTER TABLE know_posts ADD COLUMN content_origin VARCHAR(32) NOT NULL "
                        + "DEFAULT 'MANUAL' COMMENT 'MANUAL | AI_ASSISTED' AFTER status"
        );
    }

    private void addColumnIfMissing(String column, String ddl) {
        Integer count = jdbcTemplate.queryForObject(
                """
                SELECT COUNT(*)
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'know_posts'
                  AND COLUMN_NAME = ?
                """,
                Integer.class,
                column
        );
        if (count != null && count == 0) {
            jdbcTemplate.execute(ddl);
        }
    }
}
