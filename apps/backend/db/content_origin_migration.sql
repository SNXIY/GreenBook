-- Add AI/manual content provenance for GreenBook.
ALTER TABLE know_posts
    ADD COLUMN content_origin VARCHAR(32) NOT NULL DEFAULT 'MANUAL' COMMENT 'MANUAL | AI_ASSISTED' AFTER status;
