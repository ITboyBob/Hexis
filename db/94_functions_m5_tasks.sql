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

    INSERT INTO public.m5_reports (
        task_id, origin_session_id, work_execution_id
    ) VALUES (
        p_task_id, v_task.origin_session_id, p_work_execution_id
    ) ON CONFLICT (work_execution_id) DO NOTHING;

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

CREATE OR REPLACE FUNCTION public.deliver_m5_report(
    p_report_id UUID,
    p_bound_origin UUID,
    p_summary TEXT,
    p_actual_call_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_report public.m5_reports%ROWTYPE;
    v_task public.m5_tasks%ROWTYPE;
    v_work public.m5_work_rounds%ROWTYPE;
    v_origin_status TEXT;
    v_message JSONB;
    v_round JSONB;
    v_receipt JSONB;
BEGIN
    IF p_report_id IS NULL OR p_bound_origin IS NULL
       OR NULLIF(btrim(COALESCE(p_actual_call_id, '')), '') IS NULL
       OR NULLIF(btrim(COALESCE(p_summary, '')), '') IS NULL THEN
        RAISE EXCEPTION 'M5 report delivery requires bound identity and summary';
    END IF;

    SELECT * INTO v_report FROM public.m5_reports
    WHERE report_id=p_report_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'M5 report identity is unavailable';
    END IF;
    IF v_report.origin_session_id IS DISTINCT FROM p_bound_origin THEN
        RAISE EXCEPTION 'M5 report origin does not match the host binding';
    END IF;
    IF v_report.outcome='delivered' THEN
        RETURN v_report.receiver_receipt;
    END IF;
    IF v_report.outcome <> 'pending' THEN
        RAISE EXCEPTION 'M5 report is already terminal: %', v_report.outcome;
    END IF;

    SELECT * INTO v_task FROM public.m5_tasks
    WHERE id=v_report.task_id AND origin_session_id=p_bound_origin
    FOR UPDATE;
    IF NOT FOUND OR v_task.current_work_execution_id IS DISTINCT FROM v_report.work_execution_id
       OR v_task.product_state <> 'working' THEN
        RAISE EXCEPTION 'M5 report task binding is no longer active';
    END IF;
    SELECT * INTO v_work FROM public.m5_work_rounds
    WHERE id=v_report.work_execution_id AND task_id=v_report.task_id
    FOR UPDATE;
    IF NOT FOUND OR v_work.status NOT IN ('completed', 'failed', 'unknown') THEN
        RAISE EXCEPTION 'M5 report work has not ended';
    END IF;

    SELECT status INTO v_origin_status FROM public.chat_sessions
    WHERE id=p_bound_origin FOR UPDATE;
    IF NOT FOUND OR v_origin_status <> 'active' THEN
        RAISE EXCEPTION 'M5 report origin session is unavailable';
    END IF;

    v_message := public.append_chat_message(
        p_bound_origin,
        'assistant',
        p_summary,
        jsonb_build_object(
            'message_kind', 'm5_report',
            'report_id', p_report_id,
            'task_id', v_report.task_id,
            'work_execution_id', v_report.work_execution_id,
            'actual_call_id', p_actual_call_id
        ),
        'm5-report:' || p_report_id::text,
        TRUE
    );
    v_receipt := jsonb_build_object(
        'outcome', 'delivered',
        'report_id', p_report_id,
        'operation_id', p_report_id,
        'task_id', v_report.task_id,
        'origin_session_id', p_bound_origin,
        'message_id', v_message->'message_id',
        'actual_call_id', p_actual_call_id
    );
    UPDATE public.m5_reports SET
        outcome='delivered',
        summary=p_summary,
        last_call_id=p_actual_call_id,
        receiver_receipt=v_receipt,
        updated_at=clock_timestamp()
    WHERE report_id=p_report_id;

    v_round := public.finish_m5_round(
        v_report.task_id,
        v_report.work_execution_id,
        'delivered'
    );
    v_receipt := v_receipt || jsonb_build_object('round', v_round);
    UPDATE public.m5_reports SET receiver_receipt=v_receipt
    WHERE report_id=p_report_id;
    RETURN v_receipt;
END;
$$;

CREATE OR REPLACE FUNCTION public.reserve_m5_report_attempt(
    p_report_id UUID,
    p_actual_call_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_report public.m5_reports%ROWTYPE;
BEGIN
    SELECT * INTO v_report FROM public.m5_reports
    WHERE report_id=p_report_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('allowed',false,'code','REPORT_NOT_FOUND');
    END IF;
    IF v_report.outcome <> 'pending' THEN
        RETURN jsonb_build_object('allowed',false,'code',upper(v_report.outcome));
    END IF;
    IF v_report.failure_count >= 3 THEN
        RETURN jsonb_build_object('allowed',false,'code','RETRY_EXHAUSTED');
    END IF;
    IF v_report.attempt_count >= 16 THEN
        RETURN jsonb_build_object('allowed',false,'code','TOOL_ATTEMPT_LIMIT');
    END IF;
    IF NULLIF(btrim(COALESCE(p_actual_call_id,'')),'') IS NULL THEN
        RETURN jsonb_build_object('allowed',false,'code','MISSING_CALL_ID');
    END IF;
    UPDATE public.m5_reports SET
        attempt_count=attempt_count+1,
        last_call_id=p_actual_call_id,
        updated_at=clock_timestamp()
    WHERE report_id=p_report_id;
    RETURN jsonb_build_object(
        'allowed',true,
        'attempt_count',v_report.attempt_count+1,
        'failure_count',v_report.failure_count
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.record_m5_report_failure(
    p_report_id UUID,
    p_code TEXT,
    p_error TEXT,
    p_actual_call_id TEXT DEFAULT NULL,
    p_rejected_signature TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_report public.m5_reports%ROWTYPE;
    v_repeated BOOLEAN := FALSE;
    v_failures INTEGER;
    v_outcome TEXT;
    v_signatures JSONB;
    v_round JSONB;
BEGIN
    SELECT * INTO v_report FROM public.m5_reports
    WHERE report_id=p_report_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'M5 report identity is unavailable';
    END IF;
    IF v_report.outcome <> 'pending' THEN
        RETURN jsonb_build_object(
            'outcome',v_report.outcome,'failure_count',v_report.failure_count,
            'attempt_count',v_report.attempt_count,'already_terminal',TRUE,
            'receiver_receipt',v_report.receiver_receipt
        );
    END IF;

    v_signatures := COALESCE(v_report.rejected_signatures,'[]'::jsonb);
    IF p_rejected_signature IS NOT NULL THEN
        v_repeated := v_signatures @> jsonb_build_array(p_rejected_signature);
        IF NOT v_repeated THEN
            v_signatures := v_signatures || jsonb_build_array(p_rejected_signature);
        END IF;
    END IF;
    v_failures := LEAST(3,v_report.failure_count+1);
    v_outcome := CASE
        WHEN v_repeated THEN 'refused'
        WHEN v_failures >= 3 OR v_report.attempt_count >= 16 THEN 'exhausted'
        ELSE 'pending'
    END;
    UPDATE public.m5_reports SET
        failure_count=v_failures,
        rejected_signatures=v_signatures,
        outcome=v_outcome,
        last_call_id=COALESCE(NULLIF(btrim(p_actual_call_id),''),last_call_id),
        last_error=jsonb_build_object('code',p_code,'message',p_error)::text,
        updated_at=clock_timestamp()
    WHERE report_id=p_report_id;

    IF v_outcome <> 'pending' THEN
        v_round := public.finish_m5_round(v_report.task_id,v_report.work_execution_id,v_outcome);
    END IF;
    RETURN jsonb_build_object(
        'outcome',v_outcome,'code',CASE WHEN v_repeated THEN 'REPEATED_REJECTION'
                                        WHEN v_outcome='exhausted' AND v_report.attempt_count >= 16
                                            THEN 'TOOL_ATTEMPT_LIMIT'
                                        WHEN v_outcome='exhausted' THEN 'RETRY_EXHAUSTED'
                                        ELSE p_code END,
        'failure_count',v_failures,'attempt_count',v_report.attempt_count,
        'repeated_signature',v_repeated,'round',v_round
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.finish_m5_report_terminal(
    p_report_id UUID,
    p_outcome TEXT,
    p_code TEXT,
    p_error TEXT,
    p_actual_call_id TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_report public.m5_reports%ROWTYPE;
    v_round JSONB;
BEGIN
    IF p_outcome NOT IN ('exhausted','refused') THEN
        RAISE EXCEPTION 'invalid M5 report terminal outcome: %',p_outcome;
    END IF;
    SELECT * INTO v_report FROM public.m5_reports
    WHERE report_id=p_report_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'M5 report identity is unavailable'; END IF;
    IF v_report.outcome='delivered' THEN
        RETURN jsonb_build_object('outcome','delivered','receiver_receipt',v_report.receiver_receipt);
    END IF;
    IF v_report.outcome IN ('exhausted','refused') THEN
        RETURN jsonb_build_object('outcome',v_report.outcome,'already_terminal',TRUE);
    END IF;
    UPDATE public.m5_reports SET
        outcome=p_outcome,
        last_call_id=COALESCE(NULLIF(btrim(p_actual_call_id),''),last_call_id),
        last_error=jsonb_build_object('code',p_code,'message',p_error)::text,
        updated_at=clock_timestamp()
    WHERE report_id=p_report_id;
    v_round := public.finish_m5_round(v_report.task_id,v_report.work_execution_id,p_outcome);
    RETURN jsonb_build_object('outcome',p_outcome,'code',p_code,'round',v_round);
END;
$$;

-- S8: persist lightweight memory parameters separately from host authorization.
CREATE OR REPLACE FUNCTION public.submit_m5_lightweight(
    p_origin_session_id UUID,
    p_command_id UUID,
    p_tool_name TEXT,
    p_arguments JSONB,
    p_authorization JSONB
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_origin_status TEXT;
    v_actual_call_id TEXT;
    v_digest TEXT;
    v_saved public.m5_actions%ROWTYPE;
    v_result JSONB;
    v_inserted UUID;
BEGIN
    IF p_origin_session_id IS NULL OR p_command_id IS NULL
       OR p_tool_name NOT IN ('remember','add_evidence')
       OR jsonb_typeof(COALESCE(p_arguments,'null'::jsonb)) <> 'object'
       OR jsonb_typeof(COALESCE(p_authorization,'null'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'M5 lightweight submit requires origin, command, allowed tool, arguments, and authorization';
    END IF;
    v_actual_call_id := NULLIF(btrim(p_authorization->>'actual_call_id'),'');
    v_digest := p_authorization->>'argument_digest';
    IF v_actual_call_id IS NULL
       OR p_authorization->>'origin_session_id' IS DISTINCT FROM p_origin_session_id::text
       OR p_authorization->>'command_id' IS DISTINCT FROM p_command_id::text
       OR v_digest IS NULL OR v_digest !~ '^[0-9a-f]{64}$'
       OR p_authorization->>'input_turn_id' IS NULL
       OR p_authorization->>'response_id' IS NULL
       OR p_authorization->>'supplier_response_id' IS NULL THEN
        RAISE EXCEPTION 'M5 lightweight authorization is incomplete or mismatched';
    END IF;

    SELECT * INTO v_saved FROM public.m5_actions
    WHERE command_id=p_command_id FOR UPDATE;
    IF FOUND THEN
        IF v_saved.origin_session_id IS DISTINCT FROM p_origin_session_id
           OR v_saved.tool_name IS DISTINCT FROM p_tool_name
           OR v_saved.argument_digest IS DISTINCT FROM v_digest THEN
            RETURN jsonb_build_object(
                'ok',false,'state','failed','operation_id',p_command_id,
                'data','{}'::jsonb,
                'error',jsonb_build_object(
                    'code','IDEMPOTENCY_CONFLICT',
                    'message','This command id was already used for different business arguments.',
                    'retryable',false
                )
            );
        END IF;
        IF v_saved.result IS NOT NULL THEN
            RETURN v_saved.result || jsonb_build_object('replayed',true);
        END IF;
        RETURN jsonb_build_object(
            'ok',false,'state','unknown','operation_id',p_command_id,
            'data','{}'::jsonb,
            'error',jsonb_build_object(
                'code','EXECUTION_UNKNOWN',
                'message','The lightweight action has no durable result.',
                'retryable',false
            )
        );
    END IF;

    SELECT status INTO v_origin_status FROM public.chat_sessions
    WHERE id=p_origin_session_id FOR UPDATE;
    IF NOT FOUND OR v_origin_status <> 'active' THEN
        RETURN jsonb_build_object(
            'ok',false,'state','failed','operation_id',p_command_id,
            'data','{}'::jsonb,
            'error',jsonb_build_object(
                'code','ORIGIN_SESSION_UNAVAILABLE',
                'message','The source chat is unavailable.',
                'retryable',false
            )
        );
    END IF;

    v_result := jsonb_build_object(
        'ok',true,'state','accepted','operation_id',p_command_id,
        'data','{}'::jsonb,'error',NULL
    );
    INSERT INTO public.m5_actions(
        command_id,origin_session_id,tool_name,argument_digest,arguments,
        "authorization",actual_call_id,state,result
    ) VALUES (
        p_command_id,p_origin_session_id,p_tool_name,v_digest,p_arguments,
        p_authorization,v_actual_call_id,'accepted',v_result
    ) ON CONFLICT (command_id) DO NOTHING
    RETURNING command_id INTO v_inserted;
    IF v_inserted IS NOT NULL THEN
        RETURN v_result;
    END IF;

    SELECT * INTO v_saved FROM public.m5_actions
    WHERE command_id=p_command_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'M5 lightweight command conflict has no durable action: %',p_command_id;
    END IF;
    IF v_saved.origin_session_id IS DISTINCT FROM p_origin_session_id
       OR v_saved.tool_name IS DISTINCT FROM p_tool_name
       OR v_saved.argument_digest IS DISTINCT FROM v_digest THEN
        RETURN jsonb_build_object(
            'ok',false,'state','failed','operation_id',p_command_id,
            'data','{}'::jsonb,
            'error',jsonb_build_object(
                'code','IDEMPOTENCY_CONFLICT',
                'message','This command id was already used for different business arguments.',
                'retryable',false
            )
        );
    END IF;
    RETURN COALESCE(v_saved.result,v_result) || jsonb_build_object('replayed',true);
END;
$$;

CREATE OR REPLACE FUNCTION public.claim_m5_lightweight_action()
RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_action public.m5_actions%ROWTYPE;
BEGIN
    SELECT * INTO v_action FROM public.m5_actions
    WHERE tool_name IN ('remember','add_evidence')
      AND state='accepted'
      AND execution_started_at IS NULL
      AND arguments IS NOT NULL
    ORDER BY created_at,command_id
    LIMIT 1 FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    UPDATE public.m5_actions SET
        execution_started_at=clock_timestamp(),
        updated_at=clock_timestamp()
    WHERE command_id=v_action.command_id
      AND state='accepted' AND execution_started_at IS NULL;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN jsonb_build_object(
        'command_id',v_action.command_id,
        'origin_session_id',v_action.origin_session_id,
        'tool_name',v_action.tool_name,
        'arguments',v_action.arguments,
        'authorization',v_action."authorization",
        'actual_call_id',v_action.actual_call_id,
        'execution_started_at',clock_timestamp()
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.finish_m5_lightweight_action(
    p_command_id UUID,
    p_state TEXT,
    p_result JSONB
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_action public.m5_actions%ROWTYPE;
BEGIN
    IF p_state NOT IN ('done','failed','unknown')
       OR jsonb_typeof(COALESCE(p_result,'null'::jsonb)) <> 'object'
       OR p_result->>'state' IS DISTINCT FROM p_state
       OR p_result->>'operation_id' IS DISTINCT FROM p_command_id::text THEN
        RAISE EXCEPTION 'M5 lightweight terminal result is invalid';
    END IF;
    SELECT * INTO v_action FROM public.m5_actions
    WHERE command_id=p_command_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'M5 lightweight action not found: %',p_command_id;
    END IF;
    IF v_action.state IN ('done','failed','unknown') THEN
        RETURN COALESCE(v_action.result,'{}'::jsonb) || jsonb_build_object('replayed',true);
    END IF;
    IF v_action.tool_name NOT IN ('remember','add_evidence')
       OR v_action.state <> 'accepted'
       OR v_action.execution_started_at IS NULL THEN
        RETURN jsonb_build_object(
            'ok',false,'state','unknown','operation_id',p_command_id,
            'data','{}'::jsonb,
            'error',jsonb_build_object(
                'code','EXECUTION_UNKNOWN',
                'message','The lightweight action was not owned by an active claim.',
                'retryable',false
            )
        );
    END IF;
    UPDATE public.m5_actions SET
        state=p_state,
        result=p_result,
        updated_at=clock_timestamp()
    WHERE command_id=p_command_id;
    RETURN p_result;
END;
$$;
