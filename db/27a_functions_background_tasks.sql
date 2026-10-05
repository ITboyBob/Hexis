-- =============================================================
-- Background Task Functions
-- =============================================================

SET search_path = public, ag_catalog, "$user";

CREATE OR REPLACE FUNCTION create_background_task(
    p_session_id UUID,
    p_command_id UUID,
    p_input JSONB
) RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    v_id UUID;
    v_existing background_tasks%ROWTYPE;
BEGIN
    INSERT INTO background_tasks (session_id, command_id, input)
    VALUES (p_session_id, p_command_id, p_input)
    ON CONFLICT (command_id) DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT * INTO v_existing FROM background_tasks
        WHERE command_id = p_command_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'background task insert conflict without existing row: %', p_command_id;
        END IF;
        IF v_existing.session_id IS DISTINCT FROM p_session_id
           OR v_existing.input IS DISTINCT FROM p_input THEN
            RAISE EXCEPTION 'background task command_id conflict: %', p_command_id;
        END IF;
        v_id := v_existing.id;
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
    UPDATE background_tasks
    SET status = 'running',
        started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_task_id
      AND status IN ('queued', 'running', 'unknown');

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    INSERT INTO background_task_executions (task_id)
    VALUES (p_task_id)
    RETURNING id INTO v_execution_id;

    UPDATE background_tasks
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
    v_task background_tasks%ROWTYPE;
BEGIN
    SELECT * INTO v_task FROM background_tasks
    WHERE id = p_task_id FOR UPDATE;

    IF NOT FOUND
       OR v_task.current_execution_id IS DISTINCT FROM p_execution_id
       OR v_task.status NOT IN ('running', 'unknown') THEN
        RETURN FALSE;
    END IF;

    UPDATE background_task_executions
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

    UPDATE background_tasks
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
    v_task background_tasks%ROWTYPE;
BEGIN
    SELECT * INTO v_task FROM background_tasks
    WHERE id = p_task_id FOR UPDATE;

    IF NOT FOUND
       OR v_task.current_execution_id IS DISTINCT FROM p_execution_id
       OR v_task.status NOT IN ('running', 'unknown') THEN
        RETURN FALSE;
    END IF;

    UPDATE background_task_executions
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

    UPDATE background_tasks
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
    v_task background_tasks%ROWTYPE;
BEGIN
    SELECT * INTO v_task FROM background_tasks
    WHERE id = p_task_id FOR UPDATE;

    IF NOT FOUND
       OR v_task.current_execution_id IS DISTINCT FROM p_execution_id
       OR v_task.status NOT IN ('running', 'unknown') THEN
        RETURN FALSE;
    END IF;

    UPDATE background_task_executions
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

    UPDATE background_tasks
    SET status = 'unknown',
        result = NULL,
        error = p_error,
        completed_at = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_task_id;

    RETURN TRUE;
END;
$$;
