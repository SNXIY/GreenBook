CREATE TABLE IF NOT EXISTS notifications (
    id BIGINT UNSIGNED NOT NULL,
    event_id VARCHAR(128) NOT NULL,
    receiver_id BIGINT UNSIGNED NOT NULL,
    actor_id BIGINT UNSIGNED NULL,
    latest_actor_id BIGINT UNSIGNED NULL,
    type VARCHAR(32) NOT NULL,
    target_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(64) NOT NULL,
    aggregate_type VARCHAR(32) NULL,
    aggregate_id VARCHAR(64) NULL,
    title VARCHAR(128) NOT NULL,
    content VARCHAR(512) NULL,
    extra_json JSON NULL,
    actor_count INT NOT NULL DEFAULT 1,
    read_status TINYINT(1) NOT NULL DEFAULT 0,
    create_time DATETIME(3) NOT NULL,
    update_time DATETIME(3) NOT NULL,
    read_time DATETIME(3) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_notifications_event_receiver (event_id, receiver_id),
    KEY ix_notifications_receiver_ct (receiver_id, create_time, id),
    KEY ix_notifications_receiver_read_ct (receiver_id, read_status, create_time),
    KEY ix_notifications_aggregate_window (receiver_id, type, target_type, target_id, create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS notification_dedup (
    event_id VARCHAR(128) NOT NULL,
    receiver_id BIGINT UNSIGNED NOT NULL,
    create_time DATETIME(3) NOT NULL,
    PRIMARY KEY (event_id, receiver_id),
    KEY ix_notification_dedup_ct (create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
