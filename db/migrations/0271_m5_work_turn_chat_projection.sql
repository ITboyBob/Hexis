-- BOB-107 S4.1：将 M5 工作来源绑定到已保存输入与真实会话。
SET search_path = public, ag_catalog, "$user";

CREATE OR REPLACE FUNCTION start_agent_turn(
    p_mode TEXT,
    p_user_message TEXT,
    p_session_id UUID DEFAULT NULL,
    p_context JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    new_turn_id UUID;
    initial_messages JSONB;
    runtime JSONB;
    chat_projection BOOLEAN := COALESCE(
        (p_context#>>'{runtime_state,chat_message_projection}')::boolean,
        FALSE
    );
    chat_source TEXT := NULLIF(p_context#>>'{runtime_state,chat_source_identity}', '');
    chat_client UUID;
    chat_work_id_text TEXT := NULLIF(p_context#>>'{runtime_state,chat_work_execution_id}', '');
    chat_work_id UUID;
    chat_work_task_id UUID;
    chat_work_input TEXT;
BEGIN
    IF chat_projection THEN
        IF COALESCE(NULLIF(p_mode, ''), 'chat') <> 'chat' OR p_session_id IS NULL THEN
            RAISE EXCEPTION 'chat_message_projection requires a chat turn and session';
        END IF;
        IF chat_work_id_text IS NOT NULL THEN
            BEGIN
                chat_work_id := chat_work_id_text::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'chat_work_execution_id must be a UUID';
            END;
            IF chat_source IS DISTINCT FROM format('m5-work:%s', chat_work_id) THEN
                RAISE EXCEPTION 'M5 work source identity does not match chat_work_execution_id';
            END IF;
            SELECT task.id, work.input
            INTO chat_work_task_id, chat_work_input
            FROM public.m5_work_rounds AS work
            JOIN public.m5_tasks AS task ON task.id=work.task_id
            WHERE work.id=chat_work_id
              AND task.task_session_id=p_session_id
              AND task.current_work_execution_id=work.id
              AND task.product_state='working'
              AND work.status='running';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'M5 work execution does not belong to this active task session';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM public.chat_messages AS input_message
                WHERE input_message.session_id=p_session_id
                  AND input_message.role='user'
                  AND input_message.source_message_id=format('m5-work:%s:input', chat_work_id)
                  AND input_message.content=chat_work_input
                  AND input_message.metadata->>'m5_task_id'=chat_work_task_id::text
                  AND input_message.metadata->>'work_execution_id'=chat_work_id::text
                  AND input_message.metadata->>'message_kind'='m5_work_input'
            ) THEN
                RAISE EXCEPTION 'M5 work input is not durably saved for this execution';
            END IF;
        ELSE
            IF chat_source IS NULL OR split_part(chat_source, ':', 1) <> 'chat'
               OR split_part(chat_source, ':', 2) <> p_session_id::text THEN
                RAISE EXCEPTION 'chat_message_projection requires a matching chat_source_identity';
            END IF;
            BEGIN
                chat_client := split_part(chat_source, ':', 3)::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'chat_message_projection source identity must end in a client UUID';
            END;
            IF chat_source <> format('chat:%s:%s', p_session_id, chat_client) THEN
                RAISE EXCEPTION 'chat_message_projection source identity is malformed';
            END IF;
        END IF;
    ELSIF chat_work_id_text IS NOT NULL THEN
        RAISE EXCEPTION 'chat_work_execution_id requires chat_message_projection';
    END IF;
    initial_messages := COALESCE(p_context->'messages', '[]'::jsonb);
    runtime := jsonb_build_object(
        'iterations', 0,
        'energy_spent', 0,
        'energy_budget', p_context->'energy_budget',
        'max_iterations', p_context->'max_iterations',
        'continuations_used', 0,
        'max_continuations', COALESCE(NULLIF(p_context->>'max_continuations', '')::int, 0),
        'last_text', '',
        'tool_calls_made', '[]'::jsonb,
        'phases_completed', '[]'::jsonb
    ) || COALESCE(p_context->'runtime_state', '{}'::jsonb);

    INSERT INTO agent_turns (
        mode, session_id, heartbeat_id, user_message, messages, runtime_state, phase, status
    )
    VALUES (
        COALESCE(NULLIF(p_mode, ''), 'chat'),
        p_session_id,
        _db_brain_try_uuid(p_context->>'heartbeat_id'),
        p_user_message,
        initial_messages,
        runtime,
        COALESCE(NULLIF(p_context->>'phase', ''), 'execute'),
        'running'
    )
    RETURNING id INTO new_turn_id;

    IF chat_work_id IS NOT NULL THEN
        UPDATE public.m5_work_rounds AS work
        SET turn_id = new_turn_id
        WHERE work.id = chat_work_id
          AND work.task_id = chat_work_task_id
          AND work.status = 'running'
          AND work.turn_id IS NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'active M5 work execution could not be bound to the new AgentLoop turn';
        END IF;
    END IF;

    PERFORM record_agent_turn_event(new_turn_id, 'loop_start', p_context);
    RETURN jsonb_build_object('turn_id', new_turn_id::text, 'status', 'running', 'runtime_state', runtime);
END;
$$;

-- 保留 start_agent_turn 首次建立的真实 M5 turn 绑定，不允许完成回调覆盖。
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
    SELECT * INTO v_task FROM public.m5_tasks
    WHERE id=p_task_id FOR UPDATE;
    IF NOT FOUND OR v_task.product_state <> 'working'
       OR v_task.current_work_execution_id IS DISTINCT FROM p_work_execution_id THEN
        RETURN FALSE;
    END IF;
    SELECT * INTO v_work FROM public.m5_work_rounds
    WHERE id=p_work_execution_id AND task_id=p_task_id FOR UPDATE;
    IF NOT FOUND OR v_work.status <> 'running' THEN
        RETURN FALSE;
    END IF;
    IF v_work.turn_id IS NOT NULL
       AND p_turn_id IS NOT NULL
       AND p_turn_id IS DISTINCT FROM v_work.turn_id THEN
        RAISE EXCEPTION 'M5 work execution is already bound to a different AgentLoop turn';
    END IF;

    UPDATE public.m5_work_rounds
    SET status=p_status,
        turn_id=COALESCE(v_work.turn_id,p_turn_id),
        result=CASE WHEN p_status='completed' THEN COALESCE(p_result,'{}'::jsonb) ELSE NULL END,
        error=CASE WHEN p_status='completed' THEN NULL ELSE p_error END,
        finished_at=clock_timestamp()
    WHERE id=p_work_execution_id AND task_id=p_task_id;
    INSERT INTO public.m5_reports(task_id,origin_session_id,work_execution_id)
    VALUES (p_task_id,v_task.origin_session_id,p_work_execution_id)
    ON CONFLICT (work_execution_id) DO NOTHING;

    UPDATE public.m5_tasks SET revision=revision+1,updated_at=clock_timestamp()
    WHERE id=p_task_id;
    SELECT COALESCE(metadata->'m5_work_contexts','{}'::jsonb)
    INTO v_contexts FROM public.chat_sessions
    WHERE id=v_task.task_session_id FOR UPDATE;
    v_contexts := v_contexts - p_work_execution_id::text;
    IF v_contexts='{}'::jsonb THEN
        UPDATE public.chat_sessions
        SET metadata=COALESCE(metadata,'{}'::jsonb)-'m5_work_contexts',updated_at=clock_timestamp()
        WHERE id=v_task.task_session_id;
    ELSE
        UPDATE public.chat_sessions
        SET metadata=jsonb_set(COALESCE(metadata,'{}'::jsonb),'{m5_work_contexts}',v_contexts,TRUE),
            updated_at=clock_timestamp()
        WHERE id=v_task.task_session_id;
    END IF;
    RETURN TRUE;
END;
$$;
