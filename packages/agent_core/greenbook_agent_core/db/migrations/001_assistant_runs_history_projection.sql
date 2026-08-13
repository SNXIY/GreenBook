-- Phase 11.6-D8.3
-- Runtime-backed requests must not use assistant_runs.status as state.
-- Apply once against the Assistant PostgreSQL database.
ALTER TABLE assistant_runs
    ALTER COLUMN status DROP NOT NULL;
