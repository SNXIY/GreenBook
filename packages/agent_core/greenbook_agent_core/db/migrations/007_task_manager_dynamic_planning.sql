-- Phase4: canonical Task lifecycle, GoalTree projection, and plan history.
ALTER TABLE assistant_tasks
    ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS task_type VARCHAR(64) DEFAULT 'GOAL_DRIVEN',
    ADD COLUMN IF NOT EXISTS execution_mode VARCHAR(32) DEFAULT 'AUTO',
    ADD COLUMN IF NOT EXISTS root_goal_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS goal_tree_version INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS goal_tree_snapshot JSONB,
    ADD COLUMN IF NOT EXISTS plan_version INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS plan_history JSONB,
    ADD COLUMN IF NOT EXISTS active_execution_id VARCHAR(128);
