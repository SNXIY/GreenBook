ALTER TABLE outbox
    ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'NEW' AFTER payload,
    ADD COLUMN retry_count INT NOT NULL DEFAULT 0 AFTER status,
    ADD COLUMN last_error VARCHAR(512) NULL AFTER retry_count,
    ADD COLUMN published_at TIMESTAMP(3) NULL AFTER created_at,
    ADD COLUMN updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) AFTER published_at,
    ADD KEY ix_outbox_status_ct (status, created_at);
