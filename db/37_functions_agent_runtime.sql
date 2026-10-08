-- DB-owned agent-loop state and external-call dispatch helpers.
SET search_path = public, ag_catalog, "$user";

CREATE OR REPLACE FUNCTION record_agent_turn_event(
    p_turn_id UUID,
    p_event_type TEXT,
    p_payload JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    event_id UUID;
BEGIN
    INSERT INTO agent_turn_events (turn_id, event_type, payload)
    VALUES (p_turn_id, p_event_type, COALESCE(p_payload, '{}'::jsonb))
    RETURNING id INTO event_id;
    RETURN jsonb_build_object('event_id', event_id::text);
END;
$$;

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

CREATE OR REPLACE FUNCTION next_agent_step(
    p_turn_id UUID
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    turn agent_turns%ROWTYPE;
    iterations INT;
    max_iterations INT;
    energy_spent INT;
    energy_budget INT;
BEGIN
    SELECT * INTO turn FROM agent_turns WHERE id = p_turn_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent turn not found: %', p_turn_id;
    END IF;
    IF turn.status <> 'running' THEN
        RETURN jsonb_build_object('action', 'done', 'status', turn.status, 'reason', turn.stopped_reason);
    END IF;

    iterations := COALESCE(NULLIF(turn.runtime_state->>'iterations', '')::int, 0);
    BEGIN max_iterations := NULLIF(turn.runtime_state->>'max_iterations', '')::int;
    EXCEPTION WHEN OTHERS THEN max_iterations := NULL; END;
    energy_spent := COALESCE(NULLIF(turn.runtime_state->>'energy_spent', '')::int, 0);
    BEGIN energy_budget := NULLIF(turn.runtime_state->>'energy_budget', '')::int;
    EXCEPTION WHEN OTHERS THEN energy_budget := NULL; END;

    IF max_iterations IS NOT NULL AND iterations >= max_iterations THEN
        UPDATE agent_turns
        SET stopped_reason = 'max_iterations',
            runtime_state = jsonb_set(runtime_state, '{stop_decision}', '"max_iterations"', true),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = p_turn_id;
        RETURN jsonb_build_object('action', 'stop', 'reason', 'max_iterations', 'iterations', iterations, 'energy_spent', energy_spent);
    END IF;
    IF energy_budget IS NOT NULL AND energy_spent >= energy_budget THEN
        PERFORM record_agent_turn_event(p_turn_id, 'energy_exhausted', jsonb_build_object('budget', energy_budget, 'spent', energy_spent));
        UPDATE agent_turns
        SET stopped_reason = 'energy',
            runtime_state = jsonb_set(runtime_state, '{stop_decision}', '"energy"', true),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = p_turn_id;
        RETURN jsonb_build_object('action', 'stop', 'reason', 'energy', 'iterations', iterations, 'energy_spent', energy_spent);
    END IF;

    -- Hand the caller the DB-authoritative message log to send to the model.
    -- The loop reads this back each step instead of keeping a parallel Python list.
    RETURN jsonb_build_object(
        'action', 'llm',
        'iteration', iterations + 1,
        'energy_spent', energy_spent,
        'messages', COALESCE(turn.messages, '[]'::jsonb)
    );
END;
$$;

CREATE OR REPLACE FUNCTION apply_agent_llm_result(
    p_turn_id UUID,
    p_result JSONB
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    turn agent_turns%ROWTYPE;
    unlocked_turn agent_turns%ROWTYPE;
    content TEXT := COALESCE(p_result->>'content', '');
    tool_calls JSONB := COALESCE(p_result->'tool_calls', '[]'::jsonb);
    iterations INT;
    message JSONB;
    runtime JSONB;
    call JSONB;
    call_args JSONB;
    call_id TEXT;
    call_name TEXT;
BEGIN
    SELECT * INTO unlocked_turn FROM agent_turns WHERE id = p_turn_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent turn not found: %', p_turn_id;
    END IF;
    IF COALESCE((unlocked_turn.runtime_state->>'chat_message_projection')::boolean, FALSE) THEN
        IF unlocked_turn.session_id IS NULL
           OR NULLIF(unlocked_turn.runtime_state->>'chat_source_identity', '') IS NULL THEN
            RAISE EXCEPTION 'chat_message_projection turn lost its session or source identity';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext(unlocked_turn.session_id::text));
    END IF;
    SELECT * INTO turn FROM agent_turns WHERE id = p_turn_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent turn not found: %', p_turn_id;
    END IF;
    iterations := COALESCE(NULLIF(turn.runtime_state->>'iterations', '')::int, 0) + 1;
    message := jsonb_build_object('role', 'assistant', 'content', content);
    IF jsonb_typeof(tool_calls) = 'array' AND jsonb_array_length(tool_calls) > 0 THEN
        message := message || jsonb_build_object('tool_calls', tool_calls);
    END IF;
    runtime := jsonb_set(turn.runtime_state, '{iterations}', to_jsonb(iterations), true);
    runtime := jsonb_set(runtime, '{last_text}', to_jsonb(content), true);
    runtime := jsonb_set(runtime, '{last_tool_calls}', tool_calls, true);

    IF COALESCE((turn.runtime_state->>'chat_message_projection')::boolean, FALSE)
       AND jsonb_typeof(tool_calls) = 'array' THEN
        FOR call IN SELECT value FROM jsonb_array_elements(tool_calls)
        LOOP
            call_id := NULLIF(call->>'id', '');
            call_name := NULLIF(call#>>'{function,name}', '');
            IF call_id IS NULL OR call_name IS NULL THEN
                RAISE EXCEPTION 'chat toolCall requires an actual call id and function name';
            END IF;
            BEGIN
                IF jsonb_typeof(call#>'{function,arguments}') = 'object' THEN
                    call_args := call#>'{function,arguments}';
                ELSE
                    call_args := (call#>>'{function,arguments}')::jsonb;
                END IF;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'chat toolCall arguments must be valid JSON object';
            END;
            IF jsonb_typeof(call_args) <> 'object' THEN
                RAISE EXCEPTION 'chat toolCall arguments must be a JSON object';
            END IF;
            PERFORM append_chat_message_blocks(
                turn.session_id,
                'assistant',
                '',
                jsonb_build_array(jsonb_build_object(
                    'type', 'toolCall', 'id', call_id,
                    'name', call_name, 'arguments', call_args
                )),
                jsonb_build_object('agent_turn_id', turn.id::text),
                format('tool:%s:%s:call', turn.id, call_id),
                TRUE
            );
        END LOOP;
    END IF;

    UPDATE agent_turns
    SET messages = COALESCE(messages, '[]'::jsonb) || jsonb_build_array(message),
        runtime_state = runtime,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_turn_id;
    PERFORM record_agent_turn_event(p_turn_id, 'llm_result', p_result || jsonb_build_object('iteration', iterations));

    RETURN jsonb_build_object('turn_id', p_turn_id::text, 'iterations', iterations, 'tool_call_count', COALESCE(jsonb_array_length(tool_calls), 0));
END;
$$;

CREATE OR REPLACE FUNCTION apply_agent_tool_result(
    p_turn_id UUID,
    p_tool_call_id TEXT,
    p_result JSONB
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    turn agent_turns%ROWTYPE;
    unlocked_turn agent_turns%ROWTYPE;
    spent INT := COALESCE(NULLIF(p_result->>'energy_spent', '')::int, 0);
    total_spent INT;
    energy_budget INT;
    call_record JSONB;
    runtime JSONB;
    prior_result JSONB;
    output_value JSONB := p_result->'output';
    output_kind TEXT := jsonb_typeof(p_result->'output');
    result_text TEXT;
    result_details JSONB;
    tool_name TEXT := COALESCE(NULLIF(p_result->>'tool_name', ''), '<unknown>');
    registered_call JSONB;
    registered_args JSONB;
    visual_attachments JSONB;
    message_blocks JSONB;
    result_metadata JSONB;
BEGIN
    SELECT * INTO unlocked_turn FROM agent_turns WHERE id = p_turn_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent turn not found: %', p_turn_id;
    END IF;
    IF COALESCE((unlocked_turn.runtime_state->>'chat_message_projection')::boolean, FALSE) THEN
        IF unlocked_turn.session_id IS NULL
           OR NULLIF(unlocked_turn.runtime_state->>'chat_source_identity', '') IS NULL THEN
            RAISE EXCEPTION 'chat_message_projection turn lost its session or source identity';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext(unlocked_turn.session_id::text));
    END IF;
    SELECT * INTO turn FROM agent_turns WHERE id = p_turn_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent turn not found: %', p_turn_id;
    END IF;
    SELECT payload INTO prior_result
    FROM agent_turn_events
    WHERE turn_id = p_turn_id
      AND event_type = 'tool_result'
      AND payload->>'call_id' = p_tool_call_id
    ORDER BY created_at DESC
    LIMIT 1;
    IF prior_result IS NOT NULL THEN
        IF (prior_result - 'call_id' - 'total_energy_spent') IS DISTINCT FROM p_result THEN
            RAISE EXCEPTION 'tool result identity conflict for call %', p_tool_call_id;
        END IF;
        RETURN jsonb_build_object(
            'turn_id', p_turn_id::text,
            'energy_spent', COALESCE(NULLIF(turn.runtime_state->>'energy_spent', '')::int, 0),
            'replayed', TRUE
        );
    END IF;
    IF COALESCE((turn.runtime_state->>'chat_message_projection')::boolean, FALSE) THEN
        SELECT call.value INTO registered_call
        FROM jsonb_array_elements(COALESCE(turn.messages, '[]'::jsonb)) AS messages(message)
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(messages.message->'tool_calls', '[]'::jsonb)) AS call(value)
        WHERE messages.message->>'role' = 'assistant'
          AND call.value->>'id' = p_tool_call_id
        ORDER BY messages.message DESC
        LIMIT 1;
        IF registered_call IS NULL THEN
            RAISE EXCEPTION 'tool result has no registered call %', p_tool_call_id;
        END IF;
        BEGIN
            registered_args := (registered_call#>>'{function,arguments}')::jsonb;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'registered tool call arguments are not valid JSON';
        END;
        IF jsonb_typeof(registered_args) <> 'object'
           OR registered_call#>>'{function,name}' IS DISTINCT FROM tool_name
           OR registered_args IS DISTINCT FROM COALESCE(p_result->'arguments', '{}'::jsonb) THEN
            RAISE EXCEPTION 'tool result call identity, name, or arguments conflict';
        END IF;
    END IF;
    IF output_kind IN ('object', 'array') THEN
        result_details := output_value;
        result_text := COALESCE(p_result->>'model_output', p_result->>'display_output', p_result->>'error', '');
    ELSIF output_kind IN ('string', 'number', 'boolean') THEN
        result_details := 'null'::jsonb;
        result_text := CASE WHEN output_kind = 'string' THEN output_value#>>'{}' ELSE output_value::text END;
    ELSE
        result_details := 'null'::jsonb;
        result_text := COALESCE(p_result->>'error', p_result->>'model_output', p_result->>'display_output', '');
    END IF;
    visual_attachments := p_result#>'{metadata,model_visual_attachments}';
    result_metadata := jsonb_build_object(
        'agent_turn_id', p_turn_id::text,
        'tool_result', jsonb_build_object(
            'toolCallId', p_tool_call_id,
            'toolName', tool_name,
            'isError', NOT COALESCE((p_result->>'success')::boolean, FALSE),
            'details', result_details
        )
    );
    IF jsonb_typeof(visual_attachments) = 'array' THEN
        result_metadata := result_metadata || jsonb_build_object('model_visual_attachments', visual_attachments);
    END IF;
    message_blocks := jsonb_build_array(jsonb_build_object('type', 'text', 'text', COALESCE(result_text, '')));
    total_spent := COALESCE(NULLIF(turn.runtime_state->>'energy_spent', '')::int, 0) + spent;
    energy_budget := NULLIF(turn.runtime_state->>'energy_budget', '')::int;
    call_record := jsonb_build_object(
        'id', p_tool_call_id,
        'name', p_result->>'tool_name',
        'arguments', COALESCE(p_result->'arguments', '{}'::jsonb),
        'success', COALESCE((p_result->>'success')::boolean, false),
        'energy_spent', spent,
        'error', p_result->>'error'
    );
    runtime := jsonb_set(turn.runtime_state, '{energy_spent}', to_jsonb(total_spent), true);
    runtime := jsonb_set(runtime, '{tool_calls_made}', COALESCE(turn.runtime_state->'tool_calls_made', '[]'::jsonb) || jsonb_build_array(call_record), true);
    IF COALESCE((turn.runtime_state->>'chat_message_projection')::boolean, FALSE) THEN
        PERFORM append_chat_message_blocks(
            turn.session_id,
            'toolResult',
            COALESCE(result_text, ''),
            message_blocks,
            result_metadata,
            format('tool:%s:%s:result', turn.id, p_tool_call_id),
            TRUE
        );
    END IF;

    UPDATE agent_turns
    SET messages = COALESCE(messages, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
            'role', 'tool',
            'tool_call_id', p_tool_call_id,
            -- Budgeted turns see their remaining energy on every result (#44):
            -- the budget is a constraint the model must reason under, so it
            -- must be visible, not just enforced.
            'content', COALESCE(p_result->>'model_output', p_result->>'display_output', p_result->>'error', '')
                || CASE WHEN energy_budget IS NOT NULL
                        THEN E'\n\n[energy: ' || total_spent || '/' || energy_budget || ' spent]'
                        ELSE '' END
        )),
        runtime_state = runtime,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_turn_id;
    PERFORM record_agent_turn_event(
        p_turn_id,
        'tool_result',
        p_result || jsonb_build_object(
            'call_id', p_tool_call_id,
            'total_energy_spent', total_spent
        )
    );

    RETURN jsonb_build_object('turn_id', p_turn_id::text, 'energy_spent', total_spent, 'replayed', FALSE);
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

-- Append an arbitrary message (user/system/etc.) to the DB-owned message log.
-- Used for continuation nudges and plan/verify phase prompts so the message
-- list stays authoritative in the DB rather than in a parallel Python list.
CREATE OR REPLACE FUNCTION append_agent_message(
    p_turn_id UUID,
    p_role TEXT,
    p_content TEXT
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    turn agent_turns%ROWTYPE;
BEGIN
    UPDATE agent_turns
    SET messages = COALESCE(messages, '[]'::jsonb)
            || jsonb_build_array(jsonb_build_object(
                'role', COALESCE(NULLIF(p_role, ''), 'user'),
                'content', COALESCE(p_content, '')
            )),
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_turn_id
    RETURNING * INTO turn;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent turn not found: %', p_turn_id;
    END IF;

    RETURN jsonb_build_object('turn_id', p_turn_id::text, 'messages', turn.messages);
END;
$$;

-- Append trusted multimodal context produced inside the tool runtime. This is
-- separate from append_agent_message so arbitrary text callers cannot smuggle
-- provider-specific content shapes into the authoritative turn log.
CREATE OR REPLACE FUNCTION append_agent_visual_message(
    p_turn_id UUID,
    p_label TEXT,
    p_data_url TEXT
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    turn agent_turns%ROWTYPE;
BEGIN
    IF COALESCE(p_data_url, '') !~ '^data:image/(png|jpeg|webp|gif);base64,' THEN
        RAISE EXCEPTION 'visual message requires a supported image data URL';
    END IF;
    UPDATE agent_turns
    SET messages = COALESCE(messages, '[]'::jsonb)
            || jsonb_build_array(jsonb_build_object(
                'role', 'user',
                'content', jsonb_build_array(
                    jsonb_build_object(
                        'type', 'input_text',
                        'text', format('[Fresh tool-provided visual context: %s]',
                            COALESCE(NULLIF(p_label, ''), 'image'))
                    ),
                    jsonb_build_object(
                        'type', 'input_image',
                        'image_url', p_data_url,
                        'detail', 'auto'
                    )
                )
            )),
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_turn_id
    RETURNING * INTO turn;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent turn not found: %', p_turn_id;
    END IF;
    RETURN jsonb_build_object('turn_id', p_turn_id::text, 'messages', turn.messages);
END;
$$;

CREATE OR REPLACE FUNCTION finish_agent_turn(
    p_turn_id UUID,
    p_result JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    row_out agent_turns%ROWTYPE;
BEGIN
    UPDATE agent_turns
    SET status = COALESCE(NULLIF(p_result->>'status', ''), 'completed'),
        stopped_reason = COALESCE(NULLIF(p_result->>'stopped_reason', ''), stopped_reason, 'completed'),
        result = COALESCE(p_result, '{}'::jsonb),
        completed_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_turn_id
    RETURNING * INTO row_out;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent turn not found: %', p_turn_id;
    END IF;
    PERFORM record_agent_turn_event(p_turn_id, 'loop_end', p_result);

    -- Heartbeat turns join the conscious-episode substrate so the extraction
    -- sweep covers autonomous activity too (#37). Advisory: a mirroring
    -- failure must never break turn finalization.
    IF row_out.mode = 'heartbeat' THEN
        BEGIN
            PERFORM record_heartbeat_episode_unit(row_out);
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'heartbeat episode mirroring failed for turn %: %', p_turn_id, SQLERRM;
        END;
    END IF;

    RETURN to_jsonb(row_out);
END;
$$;

CREATE OR REPLACE FUNCTION resolve_external_call_kind(
    p_call JSONB
) RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    call_type TEXT := COALESCE(NULLIF(p_call->>'call_type', ''), NULLIF(p_call->>'type', ''));
    input JSONB := COALESCE(p_call->'input', p_call);
    think_kind TEXT;
BEGIN
    IF call_type = 'tool_use' OR input ? 'tool_name' OR input ? 'name' THEN
        RETURN jsonb_build_object('call_type', 'tool_use', 'kind', 'tool_use', 'input', input);
    END IF;
    IF call_type = 'embed' THEN
        RETURN jsonb_build_object('call_type', 'embed', 'kind', 'embed', 'input', input, 'supported', false);
    END IF;
    think_kind := COALESCE(NULLIF(input->>'kind', ''), 'heartbeat_decision');
    RETURN jsonb_build_object('call_type', 'think', 'kind', think_kind, 'input', input, 'supported', think_kind IN (
        'heartbeat_decision_rlm',
        'heartbeat_decision',
        'brainstorm_goals',
        'inquire',
        'reflect',
        'termination_confirm',
        'consent_request'
    ));
END;
$$;

CREATE OR REPLACE FUNCTION apply_think_result(
    p_call JSONB,
    p_result JSONB
) RETURNS JSONB
LANGUAGE sql
AS $$
    SELECT apply_external_call_result(p_call, p_result);
$$;

CREATE OR REPLACE FUNCTION apply_tool_use_result(
    p_call JSONB,
    p_result JSONB
) RETURNS JSONB
LANGUAGE sql
AS $$
    SELECT apply_external_call_result(p_call, p_result);
$$;

-- Renders conversations for `hexis transcript` (#54): everything is already in
-- Postgres (agent_turns for the exchange, agent_turn_events for the tool/
-- subconscious signal trace -- keyed by turn_id, a tighter join than the
-- loosely-typed tool_executions audit table); this just assembles it. Exactly
-- one of p_last / p_session_id is meaningful at a time: p_session_id (a
-- chat_sessions.id OR a heartbeat_id) returns every turn for that session in
-- order; otherwise the most recent p_last turns across all sessions/modes.
CREATE OR REPLACE FUNCTION get_agent_transcript(
    p_last INT DEFAULT 20,
    p_session_id UUID DEFAULT NULL
) RETURNS JSONB
LANGUAGE sql STABLE
AS $$
    WITH selected_turns AS (
        SELECT *
        FROM agent_turns
        WHERE p_session_id IS NULL
           OR session_id = p_session_id
           OR heartbeat_id = p_session_id
        ORDER BY created_at DESC
        LIMIT CASE WHEN p_session_id IS NULL THEN GREATEST(COALESCE(p_last, 20), 1) END
    )
    SELECT COALESCE(jsonb_agg(
        jsonb_build_object(
            'id', t.id,
            'mode', t.mode,
            'session_id', t.session_id,
            'heartbeat_id', t.heartbeat_id,
            'status', t.status,
            'stopped_reason', t.stopped_reason,
            'user_message', t.user_message,
            'reply_text', COALESCE(t.result->>'visible_text', t.result->>'text'),
            'iterations', COALESCE((t.result->>'iterations')::int, (t.runtime_state->>'iterations')::int),
            'energy_spent', COALESCE((t.result->>'energy_spent')::numeric, (t.runtime_state->>'energy_spent')::numeric),
            'created_at', t.created_at,
            'completed_at', t.completed_at,
            'events', COALESCE(ev.events, '[]'::jsonb)
        ) ORDER BY t.created_at
    ), '[]'::jsonb)
    FROM selected_turns t
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(
            jsonb_build_object(
                'event_type', e.event_type,
                'payload', e.payload,
                'created_at', e.created_at
            ) ORDER BY e.created_at
        ) AS events
        FROM agent_turn_events e
        WHERE e.turn_id = t.id
          -- The signals worth a transcript line; llm_request/response and raw
          -- text_delta streaming chunks would just be noise here.
          AND e.event_type IN (
              'tool_start', 'tool_result', 'energy_exhausted',
              'claim_flagged', 'question', 'approval_request',
              'error', 'phase_change'
          )
    ) ev ON true;
$$;
