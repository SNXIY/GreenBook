-- Evidence chunks are a rebuildable projection of canonical post content.
-- The post table remains the business truth; this table is never used to
-- decide whether a post is public or searchable.
CREATE TABLE IF NOT EXISTS post_chunks (
    chunk_id VARCHAR(128) NOT NULL,
    post_id BIGINT UNSIGNED NOT NULL,
    chunk_index INT UNSIGNED NOT NULL,
    content MEDIUMTEXT NOT NULL,
    token_count INT UNSIGNED NOT NULL DEFAULT 0,
    start_offset INT UNSIGNED NOT NULL,
    end_offset INT UNSIGNED NOT NULL,
    embedding_model VARCHAR(256) NOT NULL,
    embedding_version VARCHAR(128) NOT NULL,
    dimension INT UNSIGNED NOT NULL,
    event_version BIGINT UNSIGNED NOT NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (chunk_id),
    UNIQUE KEY uk_post_chunks_post_index (post_id, chunk_index),
    KEY ix_post_chunks_post_version (post_id, event_version),
    CONSTRAINT fk_post_chunks_post FOREIGN KEY (post_id) REFERENCES know_posts(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
