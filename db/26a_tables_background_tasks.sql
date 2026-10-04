-- =============================================================
-- Background Tasks
-- =============================================================
-- DB-authoritative task identity and execution instances. The session FK is
-- added by migration 0255 because chat_sessions is created later in the
-- baseline file order.
-- =============================================================

SET search_path = public, ag_catalog, "$user";

CREATE TABLE IF NOT EXISTS background_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    command_id UUID NOT NULL UNIQUE,
    input JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued', 'running', 'waiting_user', 'completed',
            'failed', 'cancelled', 'unknown'
        )),
    current_execution_id UUID,
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_background_tasks_status
    ON background_tasks (status)
    WHERE status IN ('queued', 'running', 'unknown');

CREATE INDEX IF NOT EXISTS idx_background_tasks_session_created
    ON background_tasks (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS background_task_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES background_tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'unknown')),
    turn_id UUID,
    result JSONB,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_background_task_executions_task_started
    ON background_task_executions (task_id, started_at DESC);
