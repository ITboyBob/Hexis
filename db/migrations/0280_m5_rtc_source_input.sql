-- RTC 任务提交只绑定已持久化音频元数据；保留文字提交的原始要求校验。
SET search_path = public, ag_catalog, "$user";
ALTER TABLE m5_tasks ADD COLUMN IF NOT EXISTS source_input JSONB
    CHECK (source_input IS NULL OR jsonb_typeof(source_input)='object');
ALTER TABLE m5_tasks ALTER COLUMN original_user_request DROP NOT NULL;
ALTER TABLE m5_tasks DROP CONSTRAINT IF EXISTS m5_tasks_original_user_request_check;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='m5_tasks'::regclass AND conname='m5_tasks_source_input_kind') THEN
        ALTER TABLE m5_tasks ADD CONSTRAINT m5_tasks_source_input_kind CHECK (
            (source_input IS NULL AND NULLIF(btrim(original_user_request),'') IS NOT NULL)
            OR (source_input IS NOT NULL AND original_user_request IS NULL
                AND COALESCE(source_input->>'input_modality','')='audio')
        );
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.submit_m5_task(
    p_origin_session_id UUID,
    p_command_id UUID,
    p_task_description TEXT,
    p_original_user_request TEXT,
    p_authorization JSONB
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_source_input JSONB;
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
       OR jsonb_typeof(COALESCE(p_authorization, 'null'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'M5 submit requires origin, command, task, original request, and authorization';
    END IF;

    IF p_original_user_request IS NULL THEN
        SELECT jsonb_build_object(
            'input_modality','audio','media_session_id',i.media_session_id,
            'provider_item_id',i.provider_item_id,'input_turn_id',i.input_turn_id
        ) INTO v_source_input
        FROM voice_rtc_inputs i
        JOIN voice_realtime_sessions s ON s.id=i.media_session_id
        WHERE i.session_id=p_origin_session_id
          AND i.input_turn_id::text=p_authorization->>'input_turn_id'
          AND i.media_session_id::text=p_authorization->>'media_session_id'
          AND i.provider_item_id=p_authorization->>'provider_item_id'
          AND i.input_order::text=p_authorization->>'input_order'
          AND p_authorization->>'stimulus_revision'='0'
          AND p_authorization->>'input_modality'='audio'
          ;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'M5 audio submit requires a persisted input in the active source session';
        END IF;
    ELSIF NULLIF(btrim(p_original_user_request),'') IS NULL
          OR p_authorization ? 'input_modality' THEN
        RAISE EXCEPTION 'M5 text submit requires a real original request';
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

    IF v_source_input IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM voice_realtime_sessions
        WHERE id=(v_source_input->>'media_session_id')::uuid AND status='active'
    ) THEN
        RAISE EXCEPTION 'M5 audio submit source media session is closed';
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
        task_description, original_user_request, source_input, product_state,
        current_work_execution_id
    ) VALUES (
        v_task_id, p_origin_session_id, v_task_session_id, p_command_id,
        p_task_description, p_original_user_request, v_source_input, 'working', NULL
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
