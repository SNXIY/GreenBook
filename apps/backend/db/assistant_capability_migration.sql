-- Short-lived, resource-scoped authority delegated by a user to Community Assistant.
-- A capability is consumed atomically so a copied JWT cannot bypass revocation or use limits.
CREATE TABLE IF NOT EXISTS assistant_capabilities (
    id VARCHAR(36) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    actions_json JSON NOT NULL,
    resources_json JSON NOT NULL,
    max_uses INT UNSIGNED NOT NULL,
    use_count INT UNSIGNED NOT NULL DEFAULT 0,
    expires_at DATETIME(3) NOT NULL,
    revoked TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    last_used_at DATETIME(3) NULL,
    PRIMARY KEY (id),
    KEY ix_assistant_capability_run (run_id, created_at),
    KEY ix_assistant_capability_user (user_id, created_at),
    KEY ix_assistant_capability_expiry (expires_at, revoked)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
