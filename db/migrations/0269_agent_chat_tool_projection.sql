-- BOB-107 S2：AgentLoop Chat 工具调用与结果的原子投影。
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
    turn_id UUID;
    initial_messages JSONB;
    runtime JSONB;
    chat_projection BOOLEAN := COALESCE(
        (p_context#>>'{runtime_state,chat_message_projection}')::boolean,
        FALSE
    );
    chat_source TEXT := NULLIF(p_context#>>'{runtime_state,chat_source_identity}', '');
    chat_client UUID;
BEGIN
    IF chat_projection THEN
        IF COALESCE(NULLIF(p_mode, ''), 'chat') <> 'chat' OR p_session_id IS NULL THEN
            RAISE EXCEPTION 'chat_message_projection requires a chat turn and session';
        END IF;
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
    RETURNING id INTO turn_id;

    PERFORM record_agent_turn_event(turn_id, 'loop_start', p_context);
    RETURN jsonb_build_object('turn_id', turn_id::text, 'status', 'running', 'runtime_state', runtime);
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

-- Append an arbitrary message (user/system/etc.) to the DB-owned message log.
-- Used for continuation nudges and plan/verify phase prompts so the message
-- list stays authoritative in the DB rather than in a parallel Python list.

CREATE OR REPLACE FUNCTION record_chat_session_turn(
    p_session_id UUID,
    p_user_text TEXT,
    p_assistant_text TEXT,
    p_surface TEXT DEFAULT 'chat',
    p_context JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    session_payload JSONB;
    user_message JSONB := NULL;
    assistant_message JSONB := NULL;
    memory_result JSONB := '{}'::jsonb;
    ctx JSONB := COALESCE(p_context, '{}'::jsonb);
    source_identity TEXT := NULLIF(ctx->>'source_identity', '');
BEGIN
    IF p_session_id IS NULL THEN
        RAISE EXCEPTION 'session_id is required';
    END IF;
    session_payload := get_or_create_chat_session(
        p_session_id,
        COALESCE(NULLIF(p_surface, ''), ctx->>'surface', 'chat'),
        NULLIF(ctx->>'external_id', ''),
        COALESCE(ctx->'session_metadata', '{}'::jsonb)
    );

    IF NULLIF(COALESCE(p_user_text, ''), '') IS NOT NULL THEN
        IF NULLIF(ctx->>'user_source_message_id', '') IS NOT NULL THEN
            user_message := append_chat_message_blocks(
                p_session_id,
                'user',
                p_user_text,
                jsonb_build_array(jsonb_build_object('type', 'text', 'text', p_user_text)),
                COALESCE(ctx->'user_metadata', '{}'::jsonb),
                NULLIF(ctx->>'user_source_message_id', ''),
                TRUE
            );
        ELSE
            user_message := append_chat_message(
                p_session_id,
                'user',
                p_user_text,
                COALESCE(ctx->'user_metadata', '{}'::jsonb),
                NULL,
                TRUE
            );
        END IF;
        -- Every chat surface (web, CLI, API) funnels here, so this is the one
        -- choke point that can mark real user contact (#112). Without it,
        -- last_user_contact only ever moved via the RabbitMQ inbox poller,
        -- leaving chat-only agents permanently reading "Never hours".
        PERFORM mark_user_contact();
    END IF;

    IF NULLIF(COALESCE(p_assistant_text, ''), '') IS NOT NULL THEN
        IF NULLIF(ctx->>'assistant_source_message_id', '') IS NOT NULL THEN
            assistant_message := append_chat_message_blocks(
                p_session_id,
                'assistant',
                p_assistant_text,
                jsonb_build_array(jsonb_build_object('type', 'text', 'text', p_assistant_text)),
                COALESCE(ctx->'assistant_metadata', '{}'::jsonb),
                NULLIF(ctx->>'assistant_source_message_id', ''),
                TRUE
            );
        ELSE
            assistant_message := append_chat_message(
                p_session_id,
                'assistant',
                p_assistant_text,
                COALESCE(ctx->'assistant_metadata', '{}'::jsonb),
                NULL,
                TRUE
            );
        END IF;
    END IF;

    IF COALESCE(p_user_text, '') <> '' OR COALESCE(p_assistant_text, '') <> '' THEN
        BEGIN
            memory_result := record_chat_turn_memory(
                p_user_text,
                p_assistant_text,
                p_session_id::text,
                source_identity,
                ctx
            );
        EXCEPTION WHEN OTHERS THEN
            memory_result := jsonb_build_object(
                'status', 'failed',
                'error', SQLERRM,
                'short_term_history_preserved', TRUE
            );
        END;
    END IF;

    RETURN jsonb_build_object(
        'session', session_payload,
        'user_message', user_message,
        'assistant_message', assistant_message,
        'memory', memory_result,
        'history', hydrate_chat_session(p_session_id)
    );
END;
$$;

