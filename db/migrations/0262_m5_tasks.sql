-- M5 submit/task/work persistence. One migration transaction installs the new substrate.
-- Durable M5 action, task, and first-work identities.
SET search_path = public, ag_catalog, "$user";

CREATE TABLE IF NOT EXISTS public.m5_actions (
    command_id UUID PRIMARY KEY,
    origin_session_id UUID NOT NULL
        REFERENCES public.chat_sessions(id) ON DELETE RESTRICT,
    tool_name TEXT NOT NULL
        CHECK (tool_name IN ('submit', 'pend_message', 'remember', 'add_evidence')),
    argument_digest TEXT NOT NULL
        CHECK (argument_digest ~ '^[0-9a-f]{64}$'),
    "authorization" JSONB NOT NULL
        CHECK (jsonb_typeof("authorization") = 'object'),
    actual_call_id TEXT NOT NULL
        CHECK (NULLIF(btrim(actual_call_id), '') IS NOT NULL),
    state TEXT NOT NULL DEFAULT 'accepted'
        CHECK (state IN (
            'accepted', 'pending', 'selection_cancelled', 'done',
            'failed', 'unknown'
        )),
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_m5_actions_accepted
    ON public.m5_actions (state, created_at)
    WHERE state = 'accepted';

CREATE TABLE IF NOT EXISTS public.m5_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_session_id UUID NOT NULL
        REFERENCES public.chat_sessions(id) ON DELETE RESTRICT,
    task_session_id UUID NOT NULL UNIQUE
        REFERENCES public.chat_sessions(id) ON DELETE RESTRICT,
    submit_command_id UUID NOT NULL UNIQUE
        REFERENCES public.m5_actions(command_id) ON DELETE RESTRICT,
    task_description TEXT NOT NULL
        CHECK (NULLIF(btrim(task_description), '') IS NOT NULL),
    original_user_request TEXT NOT NULL
        CHECK (NULLIF(btrim(original_user_request), '') IS NOT NULL),
    product_state TEXT NOT NULL DEFAULT 'working'
        CHECK (product_state IN ('working', 'idle')),
    current_work_execution_id UUID,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    next_receive_order BIGINT NOT NULL DEFAULT 0 CHECK (next_receive_order >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_m5_tasks_origin_created
    ON public.m5_tasks (origin_session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_m5_tasks_state_work
    ON public.m5_tasks (product_state, current_work_execution_id);

CREATE TABLE IF NOT EXISTS public.m5_work_rounds (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL
        REFERENCES public.m5_tasks(id) ON DELETE RESTRICT,
    message_id UUID,
    status TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed', 'running', 'completed', 'failed', 'unknown')),
    turn_id UUID REFERENCES public.agent_turns(id) ON DELETE RESTRICT,
    input TEXT NOT NULL CHECK (NULLIF(btrim(input), '') IS NOT NULL),
    result JSONB,
    error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    UNIQUE (id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_m5_work_task_started
    ON public.m5_work_rounds (task_id, started_at);

CREATE INDEX IF NOT EXISTS idx_m5_work_claimed
    ON public.m5_work_rounds (status)
    WHERE status = 'claimed';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'm5_tasks_current_work_execution_id_fkey'
          AND conrelid = 'public.m5_tasks'::regclass
    ) THEN
        ALTER TABLE public.m5_tasks
            ADD CONSTRAINT m5_tasks_current_work_execution_id_fkey
            FOREIGN KEY (current_work_execution_id)
            REFERENCES public.m5_work_rounds(id)
            ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END;
$$;

-- Atomic M5 submit and first-work claim/settlement functions.
SET search_path = public, ag_catalog, "$user";

CREATE OR REPLACE FUNCTION public.submit_m5_task(
    p_origin_session_id UUID,
    p_command_id UUID,
    p_task_description TEXT,
    p_original_user_request TEXT,
    p_authorization JSONB
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_origin_status TEXT;
    v_actual_call_id TEXT;
    v_digest TEXT;
    v_saved public.m5_actions%ROWTYPE;
    v_task_id UUID;
    v_task_session_id UUID;
    v_work_id UUID;
    v_result JSONB;
    v_inserted UUID;
BEGIN
    IF p_origin_session_id IS NULL OR p_command_id IS NULL
       OR NULLIF(btrim(COALESCE(p_task_description, '')), '') IS NULL
       OR NULLIF(btrim(COALESCE(p_original_user_request, '')), '') IS NULL
       OR jsonb_typeof(COALESCE(p_authorization, 'null'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'M5 submit requires origin, command, task, original request, and authorization';
    END IF;

    v_actual_call_id := NULLIF(btrim(p_authorization->>'actual_call_id'), '');
    v_digest := p_authorization->>'argument_digest';
    IF v_actual_call_id IS NULL
       OR p_authorization->>'origin_session_id' IS DISTINCT FROM p_origin_session_id::text
       OR p_authorization->>'command_id' IS DISTINCT FROM p_command_id::text
       OR v_digest IS NULL
       OR v_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'M5 submit authorization is incomplete or mismatched';
    END IF;

    -- Return an exact prior business result before checking live session state.
    -- Archiving the source chat must not turn a valid command replay into a new action.
    SELECT * INTO v_saved
    FROM public.m5_actions
    WHERE command_id = p_command_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_saved.origin_session_id IS DISTINCT FROM p_origin_session_id
           OR v_saved.tool_name IS DISTINCT FROM 'submit'
           OR v_saved.argument_digest IS DISTINCT FROM v_digest THEN
            RETURN jsonb_build_object(
                'accepted', false,
                'error', jsonb_build_object(
                    'code', 'IDEMPOTENCY_CONFLICT',
                    'message', 'This command id was already used for different business arguments.',
                    'retryable', false
                )
            );
        END IF;
        IF v_saved.result IS NULL THEN
            RETURN jsonb_build_object(
                'accepted', false,
                'error', jsonb_build_object(
                    'code', 'EXECUTION_UNKNOWN',
                    'message', 'The original submit has no stored business result.',
                    'retryable', false
                )
            );
        END IF;
        RETURN v_saved.result || jsonb_build_object('replayed', true);
    END IF;

    SELECT status INTO v_origin_status
    FROM public.chat_sessions
    WHERE id = p_origin_session_id
    FOR UPDATE;
    IF NOT FOUND OR v_origin_status <> 'active' THEN
        RETURN jsonb_build_object(
            'accepted', false,
            'error', jsonb_build_object(
                'code', 'ORIGIN_SESSION_UNAVAILABLE',
                'message', 'The source chat is unavailable.',
                'retryable', false
            )
        );
    END IF;

    INSERT INTO public.m5_actions (
        command_id, origin_session_id, tool_name, argument_digest,
        "authorization", actual_call_id, state
    ) VALUES (
        p_command_id, p_origin_session_id, 'submit', v_digest,
        p_authorization, v_actual_call_id, 'accepted'
    )
    ON CONFLICT (command_id) DO NOTHING
    RETURNING command_id INTO v_inserted;

    IF v_inserted IS NULL THEN
        SELECT * INTO v_saved
        FROM public.m5_actions
        WHERE command_id = p_command_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'M5 submit command conflict has no durable action: %', p_command_id;
        END IF;
        IF v_saved.origin_session_id IS DISTINCT FROM p_origin_session_id
           OR v_saved.tool_name IS DISTINCT FROM 'submit'
           OR v_saved.argument_digest IS DISTINCT FROM v_digest THEN
            RETURN jsonb_build_object(
                'accepted', false,
                'error', jsonb_build_object(
                    'code', 'IDEMPOTENCY_CONFLICT',
                    'message', 'This command id was already used for different business arguments.',
                    'retryable', false
                )
            );
        END IF;
        IF v_saved.result IS NULL THEN
            RETURN jsonb_build_object(
                'accepted', false,
                'error', jsonb_build_object(
                    'code', 'EXECUTION_UNKNOWN',
                    'message', 'The original submit has no stored business result.',
                    'retryable', false
                )
            );
        END IF;
        RETURN v_saved.result || jsonb_build_object('replayed', true);
    END IF;

    v_task_id := gen_random_uuid();
    v_task_session_id := gen_random_uuid();
    v_work_id := gen_random_uuid();
    PERFORM public.get_or_create_chat_session(
        v_task_session_id,
        'm5-task',
        NULL,
        jsonb_build_object(
            'm5_task_id', v_task_id,
            'origin_session_id', p_origin_session_id
        )
    );

    INSERT INTO public.m5_tasks (
        id, origin_session_id, task_session_id, submit_command_id,
        task_description, original_user_request, product_state,
        current_work_execution_id
    ) VALUES (
        v_task_id, p_origin_session_id, v_task_session_id, p_command_id,
        p_task_description, p_original_user_request, 'working', NULL
    );

    INSERT INTO public.m5_work_rounds (
        id, task_id, status, input
    ) VALUES (
        v_work_id, v_task_id, 'claimed', p_task_description
    );

    UPDATE public.m5_tasks
    SET current_work_execution_id = v_work_id,
        updated_at = clock_timestamp()
    WHERE id = v_task_id;

    v_result := jsonb_build_object(
        'accepted', true,
        'replayed', false,
        'operation_id', p_command_id,
        'task_id', v_task_id,
        'task_title', p_task_description,
        'task_state', 'working',
        'task_session_id', v_task_session_id,
        'work_execution_id', v_work_id
    );
    UPDATE public.m5_actions
    SET result = v_result,
        updated_at = clock_timestamp()
    WHERE command_id = p_command_id;
    RETURN v_result;
END;
$$;

CREATE OR REPLACE FUNCTION public.claim_m5_work_round(
    p_work_execution_id UUID
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_task_id UUID;
    v_task public.m5_tasks%ROWTYPE;
    v_work public.m5_work_rounds%ROWTYPE;
BEGIN
    SELECT task_id INTO v_task_id
    FROM public.m5_work_rounds
    WHERE id = p_work_execution_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_task
    FROM public.m5_tasks
    WHERE id = v_task_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_task.product_state <> 'working'
       OR v_task.current_work_execution_id IS DISTINCT FROM p_work_execution_id THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_work
    FROM public.m5_work_rounds
    WHERE id = p_work_execution_id AND task_id = v_task_id
    FOR UPDATE;
    IF NOT FOUND OR v_work.status <> 'claimed' THEN
        RETURN NULL;
    END IF;

    UPDATE public.m5_work_rounds
    SET status = 'running', started_at = clock_timestamp()
    WHERE id = p_work_execution_id;

    RETURN jsonb_build_object(
        'task_id', v_task.id,
        'work_execution_id', v_work.id,
        'origin_session_id', v_task.origin_session_id,
        'task_session_id', v_task.task_session_id,
        'task_description', v_task.task_description,
        'input', v_work.input,
        'input_context', (
            SELECT s.metadata->'m5_work_contexts'->v_work.id::text
            FROM public.chat_sessions s
            WHERE s.id=v_task.task_session_id
        ),
        'revision', v_task.revision,
        'work_status', 'running'
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.finish_m5_work_round(
    p_task_id UUID,
    p_work_execution_id UUID,
    p_status TEXT,
    p_result JSONB,
    p_error TEXT,
    p_turn_id UUID DEFAULT NULL
) RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
DECLARE
    v_task public.m5_tasks%ROWTYPE;
    v_work public.m5_work_rounds%ROWTYPE;
    v_contexts JSONB;
BEGIN
    IF p_status NOT IN ('completed', 'failed', 'unknown') THEN
        RAISE EXCEPTION 'invalid M5 work terminal status: %', p_status;
    END IF;

    SELECT * INTO v_task
    FROM public.m5_tasks
    WHERE id = p_task_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_task.product_state <> 'working'
       OR v_task.current_work_execution_id IS DISTINCT FROM p_work_execution_id THEN
        RETURN FALSE;
    END IF;

    SELECT * INTO v_work
    FROM public.m5_work_rounds
    WHERE id = p_work_execution_id AND task_id = p_task_id
    FOR UPDATE;
    IF NOT FOUND OR v_work.status <> 'running' THEN
        RETURN FALSE;
    END IF;

    UPDATE public.m5_work_rounds
    SET status = p_status,
        turn_id = p_turn_id,
        result = CASE WHEN p_status = 'completed' THEN COALESCE(p_result, '{}'::jsonb) ELSE NULL END,
        error = CASE WHEN p_status = 'completed' THEN NULL ELSE p_error END,
        finished_at = clock_timestamp()
    WHERE id = p_work_execution_id AND task_id = p_task_id;

    UPDATE public.m5_tasks
    SET revision = revision + 1,
        updated_at = clock_timestamp()
    WHERE id = p_task_id;

    SELECT COALESCE(metadata->'m5_work_contexts', '{}'::jsonb)
    INTO v_contexts
    FROM public.chat_sessions
    WHERE id = v_task.task_session_id
    FOR UPDATE;
    v_contexts := v_contexts - p_work_execution_id::text;
    IF v_contexts = '{}'::jsonb THEN
        UPDATE public.chat_sessions
        SET metadata = COALESCE(metadata, '{}'::jsonb) - 'm5_work_contexts',
            updated_at = clock_timestamp()
        WHERE id = v_task.task_session_id;
    ELSE
        UPDATE public.chat_sessions
        SET metadata = jsonb_set(
                COALESCE(metadata, '{}'::jsonb),
                '{m5_work_contexts}', v_contexts, TRUE
            ),
            updated_at = clock_timestamp()
        WHERE id = v_task.task_session_id;
    END IF;
    -- M5 stays product-state working until the separate report closes this round.
    RETURN TRUE;
END;
$$;
