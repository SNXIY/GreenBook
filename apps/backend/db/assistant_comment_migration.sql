-- Durable provenance for comments written by Community Assistant.
-- The system user itself is created idempotently by Java on first use.
CREATE TABLE IF NOT EXISTS assistant_comment_provenance (
    comment_id BIGINT UNSIGNED NOT NULL,
    assistant_run_id VARCHAR(64) NOT NULL,
    source_post_id BIGINT UNSIGNED NOT NULL,
    source_post_sha256 CHAR(64) NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (comment_id),
    UNIQUE KEY uk_assistant_comment_run (assistant_run_id),
    KEY ix_assistant_comment_post (source_post_id, created_at),
    CONSTRAINT fk_assistant_comment_comment
        FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE,
    CONSTRAINT fk_assistant_comment_post
        FOREIGN KEY (source_post_id) REFERENCES know_posts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
