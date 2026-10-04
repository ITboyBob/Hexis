-- DB-authoritative background task identity and execution instances.
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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'background_tasks_session_id_fkey'
          AND conrelid = 'public.background_tasks'::regclass
    ) THEN
        ALTER TABLE public.background_tasks
        ADD CONSTRAINT background_tasks_session_id_fkey
        FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id)
        ON DELETE CASCADE;
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_background_tasks_status
    ON public.background_tasks (status)
    WHERE status IN ('queued', 'running', 'unknown');

CREATE INDEX IF NOT EXISTS idx_background_tasks_session_created
    ON public.background_tasks (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS background_task_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES public.background_tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'unknown')),
    turn_id UUID,
    result JSONB,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_background_task_executions_task_started
    ON public.background_task_executions (task_id, started_at DESC);

CREATE OR REPLACE FUNCTION create_background_task(
    p_session_id UUID,
    p_command_id UUID,
    p_input JSONB
) RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO public.background_tasks (session_id, command_id, input)
    VALUES (p_session_id, p_command_id, p_input)
    ON CONFLICT (command_id) DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT id INTO v_id FROM public.background_tasks
        WHERE command_id = p_command_id;
    END IF;

    RETURN v_id;
END;
$$;

CREATE OR REPLACE FUNCTION claim_background_task(p_task_id UUID)
RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    v_execution_id UUID;
BEGIN
    UPDATE public.background_tasks
    SET status = 'running',
        started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_task_id
      AND status IN ('queued', 'running', 'unknown');

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    INSERT INTO public.background_task_executions (task_id)
    VALUES (p_task_id)
    RETURNING id INTO v_execution_id;

    UPDATE public.background_tasks
    SET current_execution_id = v_execution_id,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_task_id;

    RETURN v_execution_id;
END;
$$;

CREATE OR REPLACE FUNCTION complete_background_task(
    p_task_id UUID,
    p_execution_id UUID,
    p_result JSONB,
    p_turn_id UUID DEFAULT NULL
) RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
DECLARE
    v_task public.background_tasks%ROWTYPE;
BEGIN
    SELECT * INTO v_task FROM public.background_tasks
    WHERE id = p_task_id FOR UPDATE;

    IF NOT FOUND
       OR v_task.current_execution_id IS DISTINCT FROM p_execution_id
       OR v_task.status NOT IN ('running', 'unknown') THEN
        RETURN FALSE;
    END IF;

    UPDATE public.background_task_executions
    SET status = 'completed',
        result = COALESCE(p_result, '{}'::jsonb),
        error = NULL,
        turn_id = p_turn_id,
        finished_at = CURRENT_TIMESTAMP
    WHERE id = p_execution_id
      AND task_id = p_task_id
      AND status IN ('running', 'unknown');

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE public.background_tasks
    SET status = 'completed',
        result = COALESCE(p_result, '{}'::jsonb),
        error = NULL,
        completed_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_task_id;

    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION fail_background_task(
    p_task_id UUID,
    p_execution_id UUID,
    p_error TEXT,
    p_turn_id UUID DEFAULT NULL
) RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
DECLARE
    v_task public.background_tasks%ROWTYPE;
BEGIN
    SELECT * INTO v_task FROM public.background_tasks
    WHERE id = p_task_id FOR UPDATE;

    IF NOT FOUND
       OR v_task.current_execution_id IS DISTINCT FROM p_execution_id
       OR v_task.status NOT IN ('running', 'unknown') THEN
        RETURN FALSE;
    END IF;

    UPDATE public.background_task_executions
    SET status = 'failed',
        result = NULL,
        error = p_error,
        turn_id = p_turn_id,
        finished_at = CURRENT_TIMESTAMP
    WHERE id = p_execution_id
      AND task_id = p_task_id
      AND status IN ('running', 'unknown');

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE public.background_tasks
    SET status = 'failed',
        result = NULL,
        error = p_error,
        completed_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_task_id;

    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION mark_background_task_unknown(
    p_task_id UUID,
    p_execution_id UUID,
    p_error TEXT,
    p_turn_id UUID DEFAULT NULL
) RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
DECLARE
    v_task public.background_tasks%ROWTYPE;
BEGIN
    SELECT * INTO v_task FROM public.background_tasks
    WHERE id = p_task_id FOR UPDATE;

    IF NOT FOUND
       OR v_task.current_execution_id IS DISTINCT FROM p_execution_id
       OR v_task.status NOT IN ('running', 'unknown') THEN
        RETURN FALSE;
    END IF;

    UPDATE public.background_task_executions
    SET status = 'unknown',
        result = NULL,
        error = p_error,
        turn_id = p_turn_id,
        finished_at = CURRENT_TIMESTAMP
    WHERE id = p_execution_id
      AND task_id = p_task_id
      AND status IN ('running', 'unknown');

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE public.background_tasks
    SET status = 'unknown',
        result = NULL,
        error = p_error,
        completed_at = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_task_id;

    RETURN TRUE;
END;
$$;
