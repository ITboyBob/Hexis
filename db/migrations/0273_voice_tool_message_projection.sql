-- BOB-107 S5.1：允许真实语音 turn 将工具事实投影到统一会话历史。
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
    voice_source_input_id UUID;
    voice_source_revision INT;
BEGIN
    IF chat_projection THEN
        IF COALESCE(NULLIF(p_mode, ''), 'chat') NOT IN ('chat','voice') OR p_session_id IS NULL THEN
            RAISE EXCEPTION 'chat_message_projection requires a chat or voice turn and session';
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
        ELSIF COALESCE(NULLIF(p_mode, ''), 'chat') = 'voice' THEN
            IF split_part(chat_source, ':', 1) <> 'voice'
               OR split_part(chat_source, ':', 2) <> p_session_id::text THEN
                RAISE EXCEPTION 'voice chat projection requires a matching voice source identity';
            END IF;
            BEGIN
                voice_source_input_id := split_part(chat_source, ':', 3)::uuid;
                voice_source_revision := split_part(chat_source, ':', 4)::int;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'voice chat source identity requires an input UUID and revision';
            END;
            IF voice_source_revision < 0
               OR chat_source <> format(
                   'voice:%s:%s:%s', p_session_id, voice_source_input_id, voice_source_revision
               ) THEN
                RAISE EXCEPTION 'voice chat source identity is malformed';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.chat_messages AS input_message
                WHERE input_message.session_id=p_session_id
                  AND input_message.role='user'
                  AND input_message.content=p_user_message
                  AND input_message.source_message_id=chat_source || ':user'
                  AND input_message.metadata->>'channel'='voice'
                  AND input_message.metadata->>'input_turn_id'=voice_source_input_id::text
                  AND input_message.metadata->>'input_turn_id'=p_context#>>'{runtime_state,input_turn_id}'
                  AND input_message.metadata->>'input_order'=p_context#>>'{runtime_state,input_order}'
                  AND input_message.metadata->>'stimulus_revision'=voice_source_revision::text
                  AND input_message.metadata->>'context_version'=p_context#>>'{runtime_state,context_version}'
                  AND NULLIF(input_message.metadata->>'context_version','') IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'voice input is not durably saved for this source identity';
            END IF;
            PERFORM mark_user_contact();
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

CREATE OR REPLACE FUNCTION record_voice_tool_call(
    p_turn_id UUID,
    p_call_id TEXT,
    p_tool_name TEXT,
    p_arguments JSONB
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    unlocked_turn agent_turns%ROWTYPE;
    turn agent_turns%ROWTYPE;
    call_message public.chat_messages%ROWTYPE;
    result_message public.chat_messages%ROWTYPE;
    expected_blocks JSONB;
    prior_output TEXT;
BEGIN
    IF p_turn_id IS NULL OR NULLIF(btrim(COALESCE(p_call_id,'')), '') IS NULL
       OR NULLIF(btrim(COALESCE(p_tool_name,'')), '') IS NULL
       OR jsonb_typeof(p_arguments) <> 'object' THEN
        RAISE EXCEPTION 'voice tool call requires turn, call id, name, and object arguments';
    END IF;
    SELECT * INTO unlocked_turn FROM public.agent_turns WHERE id=p_turn_id;
    IF NOT FOUND OR unlocked_turn.mode <> 'voice'
       OR unlocked_turn.session_id IS NULL
       OR NOT COALESCE((unlocked_turn.runtime_state->>'chat_message_projection')::boolean,FALSE)
       OR NULLIF(unlocked_turn.runtime_state->>'chat_source_identity','') IS NULL THEN
        RAISE EXCEPTION 'voice tool call requires a projected voice turn';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext(unlocked_turn.session_id::text));
    SELECT * INTO turn FROM public.agent_turns WHERE id=p_turn_id FOR UPDATE;
    IF turn.status <> 'running' THEN
        RAISE EXCEPTION 'voice tool call turn is no longer running';
    END IF;
    expected_blocks := jsonb_build_array(jsonb_build_object(
        'type','toolCall','id',p_call_id,'name',p_tool_name,'arguments',p_arguments
    ));
    SELECT * INTO call_message FROM public.chat_messages
    WHERE session_id=turn.session_id
      AND source_message_id=format('tool:%s:%s:call',turn.id,p_call_id)
    FOR UPDATE;
    IF FOUND THEN
        IF call_message.role <> 'assistant'
           OR call_message.content <> ''
           OR call_message.content_blocks IS DISTINCT FROM expected_blocks
           OR call_message.metadata->>'agent_turn_id' IS DISTINCT FROM turn.id::text THEN
            RAISE EXCEPTION 'voice tool call identity conflicts with its durable message';
        END IF;
        SELECT * INTO result_message FROM public.chat_messages
        WHERE session_id=turn.session_id
          AND source_message_id=format('tool:%s:%s:result',turn.id,p_call_id);
        IF FOUND THEN
            IF result_message.role <> 'toolResult'
               OR result_message.metadata->>'agent_turn_id' IS DISTINCT FROM turn.id::text
               OR result_message.metadata#>>'{tool_result,toolCallId}' IS DISTINCT FROM p_call_id
               OR result_message.metadata#>>'{tool_result,toolName}' IS DISTINCT FROM p_tool_name THEN
                RAISE EXCEPTION 'voice tool result identity conflicts with its durable message';
            END IF;
            SELECT message->>'content' INTO prior_output
            FROM jsonb_array_elements(COALESCE(turn.messages,'[]'::jsonb))
                WITH ORDINALITY AS rows(message, ordinal)
            WHERE message->>'role'='tool' AND message->>'tool_call_id'=p_call_id
            ORDER BY ordinal DESC LIMIT 1;
            IF prior_output IS NULL THEN
                RAISE EXCEPTION 'durable voice tool result has no authoritative AgentLoop result';
            END IF;
            RETURN jsonb_build_object('status','completed','output',prior_output);
        END IF;
        RETURN jsonb_build_object(
            'status','unknown',
            'error','The call is already registered without a durable result; do not execute it again.'
        );
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.chat_messages
        WHERE session_id=turn.session_id AND role='toolResult'
          AND metadata->>'agent_turn_id'=turn.id::text
          AND metadata#>>'{tool_result,toolCallId}'=p_call_id
    ) THEN
        RAISE EXCEPTION 'voice tool result exists without its registered call';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(COALESCE(turn.messages,'[]'::jsonb)) AS message(value)
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(message.value->'tool_calls','[]'::jsonb)) AS call(value)
        WHERE message.value->>'role'='assistant'
          AND call.value->>'id'=p_call_id
    ) THEN
        RAISE EXCEPTION 'voice AgentLoop transcript already has an unprojected call identity';
    END IF;
    PERFORM public.apply_agent_llm_result(
        p_turn_id,
        jsonb_build_object(
            'content','',
            'tool_calls',jsonb_build_array(jsonb_build_object(
                'id',p_call_id,
                'type','function',
                'function',jsonb_build_object('name',p_tool_name,'arguments',p_arguments::text)
            ))
        )
    );
    RETURN jsonb_build_object('status','registered');
END;
$$;

CREATE OR REPLACE FUNCTION record_voice_tool_result(
    p_turn_id UUID,
    p_call_id TEXT,
    p_result JSONB
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    turn agent_turns%ROWTYPE;
BEGIN
    IF p_turn_id IS NULL OR NULLIF(btrim(COALESCE(p_call_id,'')), '') IS NULL
       OR jsonb_typeof(COALESCE(p_result,'{}'::jsonb)) <> 'object'
       OR p_result->>'call_id' IS DISTINCT FROM p_call_id THEN
        RAISE EXCEPTION 'voice tool result requires its exact call identity';
    END IF;
    SELECT * INTO turn FROM public.agent_turns WHERE id=p_turn_id;
    IF NOT FOUND OR turn.mode <> 'voice'
       OR turn.session_id IS NULL
       OR NOT COALESCE((turn.runtime_state->>'chat_message_projection')::boolean,FALSE) THEN
        RAISE EXCEPTION 'voice tool result requires a projected voice turn';
    END IF;
    RETURN public.apply_agent_tool_result(p_turn_id,p_call_id,p_result);
END;
$$;
