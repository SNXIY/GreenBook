ALTER TABLE know_posts
    ADD COLUMN event_version BIGINT UNSIGNED NOT NULL DEFAULT 0
        COMMENT 'Monotonic post mutation version for search projections'
        AFTER content_sha256;
