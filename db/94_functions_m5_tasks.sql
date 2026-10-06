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

-- S5 replacement returns the host-owned task-window context with the claim.
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

-- S5 replacement clears only this round's temporary task-window attachment context.
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

-- S5 FIFO claim and message-append path.
CREATE OR REPLACE FUNCTION public.claim_next_m5_message(
    p_task_id UUID
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_task public.m5_tasks%ROWTYPE;
    v_pending public.m5_pending_messages%ROWTYPE;
    v_work_id UUID;
BEGIN
    SELECT * INTO v_task
    FROM public.m5_tasks
    WHERE id=p_task_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_task.product_state <> 'idle'
       OR v_task.current_work_execution_id IS NOT NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_pending
    FROM public.m5_pending_messages
    WHERE task_id=p_task_id AND claimed_work_execution_id IS NULL
    ORDER BY receive_order
    LIMIT 1
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    v_work_id := gen_random_uuid();
    INSERT INTO public.m5_work_rounds (
        id, task_id, message_id, status, input
    ) VALUES (
        v_work_id, p_task_id, v_pending.id, 'claimed', v_pending.message
    );
    UPDATE public.m5_pending_messages
    SET claimed_work_execution_id=v_work_id
    WHERE id=v_pending.id AND task_id=p_task_id
      AND claimed_work_execution_id IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'pending message claim changed while task lock was held: %', v_pending.id;
    END IF;

    UPDATE public.m5_tasks
    SET product_state='working',
        current_work_execution_id=v_work_id,
        revision=revision+1,
        updated_at=clock_timestamp()
    WHERE id=p_task_id;

    RETURN jsonb_build_object(
        'task_id', p_task_id,
        'task_session_id', v_task.task_session_id,
        'work_execution_id', v_work_id,
        'message_id', v_pending.id,
        'receive_order', v_pending.receive_order,
        'task_state', 'working',
        'revision', v_task.revision+1
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.append_m5_message(
    p_origin_session_id UUID,
    p_task_id UUID,
    p_command_id UUID,
    p_message TEXT,
    p_authorization JSONB
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_task public.m5_tasks%ROWTYPE;
    v_saved public.m5_actions%ROWTYPE;
    v_origin_status TEXT;
    v_actual_call_id TEXT;
    v_digest TEXT;
    v_order BIGINT;
    v_message_id UUID;
    v_inserted UUID;
    v_claim JSONB;
    v_result JSONB;
BEGIN
    IF p_origin_session_id IS NULL OR p_task_id IS NULL OR p_command_id IS NULL
       OR NULLIF(btrim(COALESCE(p_message, '')), '') IS NULL
       OR jsonb_typeof(COALESCE(p_authorization, 'null'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'M5 pending message requires origin, task, command, message, and authorization';
    END IF;
    v_actual_call_id := NULLIF(btrim(p_authorization->>'actual_call_id'), '');
    v_digest := p_authorization->>'argument_digest';
    IF v_actual_call_id IS NULL
       OR p_authorization->>'origin_session_id' IS DISTINCT FROM p_origin_session_id::text
       OR p_authorization->>'task_id' IS DISTINCT FROM p_task_id::text
       OR p_authorization->>'command_id' IS DISTINCT FROM p_command_id::text
       OR v_digest IS NULL
       OR v_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'M5 pending authorization is incomplete or mismatched';
    END IF;

    SELECT * INTO v_task
    FROM public.m5_tasks
    WHERE id=p_task_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'accepted', false,
            'error', jsonb_build_object(
                'code', 'TARGET_NOT_FOUND',
                'message', 'The selected task no longer exists.',
                'retryable', false
            )
        );
    END IF;
    IF v_task.origin_session_id IS DISTINCT FROM p_origin_session_id THEN
        RETURN jsonb_build_object(
            'accepted', false,
            'error', jsonb_build_object(
                'code', 'TARGET_OWNERSHIP_MISMATCH',
                'message', 'The selected task does not belong to the source chat.',
                'retryable', false
            )
        );
    END IF;

    SELECT * INTO v_saved
    FROM public.m5_actions
    WHERE command_id=p_command_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_saved.origin_session_id IS DISTINCT FROM p_origin_session_id
           OR v_saved.tool_name IS DISTINCT FROM 'pend_message'
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
                    'message', 'The original pending action has no stored business result.',
                    'retryable', false
                )
            );
        END IF;
        RETURN v_saved.result || jsonb_build_object('replayed', true);
    END IF;

    SELECT status INTO v_origin_status
    FROM public.chat_sessions
    WHERE id=p_origin_session_id
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
    IF v_task.product_state NOT IN ('working', 'idle') THEN
        RETURN jsonb_build_object(
            'accepted', false,
            'error', jsonb_build_object(
                'code', 'TARGET_NOT_FOUND',
                'message', 'The selected task is not available.',
                'retryable', false
            )
        );
    END IF;

    INSERT INTO public.m5_actions (
        command_id, origin_session_id, tool_name, argument_digest,
        "authorization", actual_call_id, state
    ) VALUES (
        p_command_id, p_origin_session_id, 'pend_message', v_digest,
        p_authorization, v_actual_call_id, 'pending'
    )
    ON CONFLICT (command_id) DO NOTHING
    RETURNING command_id INTO v_inserted;
    IF v_inserted IS NULL THEN
        SELECT * INTO v_saved FROM public.m5_actions
        WHERE command_id=p_command_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'M5 pending command conflict has no durable action: %', p_command_id;
        END IF;
        IF v_saved.origin_session_id IS DISTINCT FROM p_origin_session_id
           OR v_saved.tool_name IS DISTINCT FROM 'pend_message'
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
                    'message', 'The original pending action has no stored business result.',
                    'retryable', false
                )
            );
        END IF;
        RETURN v_saved.result || jsonb_build_object('replayed', true);
    END IF;

    UPDATE public.m5_tasks
    SET next_receive_order=next_receive_order+1,
        updated_at=clock_timestamp()
    WHERE id=p_task_id
    RETURNING next_receive_order INTO v_order;
    INSERT INTO public.m5_pending_messages (
        task_id, command_id, receive_order, message
    ) VALUES (
        p_task_id, p_command_id, v_order, p_message
    ) RETURNING id INTO v_message_id;

    IF v_task.product_state = 'idle' THEN
        v_claim := public.claim_next_m5_message(p_task_id);
        IF v_claim IS NULL THEN
            RAISE EXCEPTION 'idle M5 task could not claim its persisted pending queue: %', p_task_id;
        END IF;
    END IF;
    v_result := jsonb_build_object(
        'accepted', true,
        'replayed', false,
        'operation_id', p_command_id,
        'state', 'pending',
        'task_id', p_task_id,
        'task_title', v_task.task_description,
        'message_id', v_message_id,
        'receive_order', v_order,
        'task_state', CASE WHEN v_claim IS NULL THEN v_task.product_state ELSE 'working' END,
        'work_execution_id', v_claim->'work_execution_id'
    );
    UPDATE public.m5_actions
    SET result=v_result, updated_at=clock_timestamp()
    WHERE command_id=p_command_id;
    RETURN v_result;
END;
$$;

CREATE OR REPLACE FUNCTION public.finish_m5_round(
    p_task_id UUID,
    p_work_execution_id UUID,
    p_report_outcome TEXT
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_task public.m5_tasks%ROWTYPE;
    v_work public.m5_work_rounds%ROWTYPE;
    v_next JSONB;
BEGIN
    IF p_report_outcome IS NULL
       OR p_report_outcome NOT IN ('delivered', 'exhausted', 'refused') THEN
        RAISE EXCEPTION 'invalid M5 report terminal outcome: %', p_report_outcome;
    END IF;
    SELECT * INTO v_task FROM public.m5_tasks
    WHERE id=p_task_id FOR UPDATE;
    IF NOT FOUND
       OR v_task.product_state <> 'working'
       OR v_task.current_work_execution_id IS DISTINCT FROM p_work_execution_id THEN
        RETURN jsonb_build_object('finished', false);
    END IF;
    SELECT * INTO v_work FROM public.m5_work_rounds
    WHERE id=p_work_execution_id AND task_id=p_task_id FOR UPDATE;
    IF NOT FOUND OR v_work.status NOT IN ('completed', 'failed', 'unknown') THEN
        RETURN jsonb_build_object('finished', false);
    END IF;

    UPDATE public.m5_tasks
    SET product_state='idle',
        current_work_execution_id=NULL,
        revision=revision+1,
        updated_at=clock_timestamp()
    WHERE id=p_task_id;
    v_next := public.claim_next_m5_message(p_task_id);
    RETURN jsonb_build_object(
        'finished', true,
        'task_id', p_task_id,
        'task_session_id', v_task.task_session_id,
        'report_outcome', p_report_outcome,
        'task_state', CASE WHEN v_next IS NULL THEN 'idle' ELSE 'working' END,
        'next_message_id', v_next->'message_id',
        'next_work_execution_id', v_next->'work_execution_id',
        'revision', CASE
            WHEN v_next IS NULL THEN v_task.revision+1
            ELSE (v_next->>'revision')::bigint
        END
    );
END;
$$;
