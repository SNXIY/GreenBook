-- Add content origin + moderation task tracking for GreenBook fusion
ALTER TABLE know_posts
    ADD COLUMN content_origin VARCHAR(32) NOT NULL DEFAULT 'MANUAL' COMMENT 'MANUAL | AI_ASSISTED' AFTER status,
    ADD COLUMN moderation_task_id VARCHAR(64) NULL COMMENT 'external moderation agent task id' AFTER content_origin,
    ADD COLUMN moderation_reason VARCHAR(512) NULL COMMENT 'latest moderation decision reason' AFTER moderation_task_id;
