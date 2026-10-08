-- BOB-107 S4.2：将后台工作轮次的会话正文与原始记忆绑定到稳定来源。
SET search_path = public, ag_catalog, "$user";

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
    memory_ctx JSONB;
    v_source_identity TEXT := NULLIF(btrim(COALESCE(ctx->>'source_identity', '')), '');
    existing_unit subconscious_units%ROWTYPE;
BEGIN
    IF p_session_id IS NULL THEN
        RAISE EXCEPTION 'session_id is required';
    END IF;
    IF v_source_identity IS NOT NULL
       AND (COALESCE(p_user_text, '') <> '' OR COALESCE(p_assistant_text, '') <> '') THEN
        SELECT * INTO existing_unit
        FROM public.subconscious_units
        WHERE idempotency_key = 'src:' || v_source_identity;
        IF FOUND
           AND (existing_unit.user_text IS DISTINCT FROM COALESCE(p_user_text, '')
                OR existing_unit.assistant_text IS DISTINCT FROM COALESCE(p_assistant_text, '')) THEN
            RAISE EXCEPTION 'source_identity conflict for raw conversation memory: %', v_source_identity;
        END IF;
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
        IF NOT COALESCE((ctx->>'host_background')::boolean, FALSE) THEN
            PERFORM mark_user_contact();
        END IF;
    END IF;

    IF p_assistant_text IS NOT NULL
       AND (p_assistant_text <> '' OR COALESCE((ctx->>'host_background')::boolean, FALSE)) THEN
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
            IF COALESCE((ctx->>'host_background')::boolean, FALSE) THEN
                memory_ctx := jsonb_build_object('metadata', jsonb_build_object('type', 'conversation'))
                    || CASE WHEN NULLIF(ctx->>'user_label', '') IS NULL THEN '{}'::jsonb
                            ELSE jsonb_build_object('user_label', ctx->>'user_label') END
                    || CASE WHEN ctx ? 'importance' THEN jsonb_build_object('importance', ctx->'importance') ELSE '{}'::jsonb END
                    || CASE WHEN jsonb_typeof(ctx->'emotional_state')='object' THEN jsonb_build_object('emotional_state', ctx->'emotional_state') ELSE '{}'::jsonb END
                    || CASE WHEN jsonb_typeof(ctx->'source_attribution')='object' THEN jsonb_build_object('source_attribution', ctx->'source_attribution') ELSE '{}'::jsonb END
                    || CASE WHEN NULLIF(ctx->>'source_attribution_kind', '') IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('source_attribution_kind', ctx->>'source_attribution_kind') END
                    || CASE WHEN NULLIF(ctx->>'source_attribution_label', '') IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('source_attribution_label', ctx->>'source_attribution_label') END
                    || CASE WHEN ctx ? 'trust' THEN jsonb_build_object('trust', ctx->'trust') ELSE '{}'::jsonb END
                    || CASE WHEN NULLIF(ctx->>'sensitivity', '') IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('sensitivity', ctx->>'sensitivity') END;
            ELSE
                memory_ctx := ctx;
            END IF;
            memory_result := record_chat_turn_memory(
                p_user_text,
                p_assistant_text,
                p_session_id::text,
                v_source_identity,
                memory_ctx
            );
            IF memory_result ? 'raw_unit_id' THEN
                SELECT * INTO existing_unit
                FROM public.subconscious_units
                WHERE id = _db_brain_try_uuid(memory_result->>'raw_unit_id');
                IF FOUND
                   AND (existing_unit.user_text IS DISTINCT FROM COALESCE(p_user_text, '')
                        OR existing_unit.assistant_text IS DISTINCT FROM COALESCE(p_assistant_text, '')) THEN
                    memory_result := jsonb_build_object(
                        'status', 'conflict',
                        'error', format('source_identity conflict for raw conversation memory: %s', v_source_identity),
                        'raw_unit_id', existing_unit.id::text
                    );
                END IF;
            END IF;
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
