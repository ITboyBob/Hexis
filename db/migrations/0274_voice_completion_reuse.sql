-- BOB-107 S5.2：复用已预存语音输入并保留完成门与冲突检查。
SET search_path = public, ag_catalog, "$user";

CREATE TABLE IF NOT EXISTS voice_turn_completions (
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    input_turn_id UUID NOT NULL,
    input_order INT NOT NULL CHECK (input_order >= 0),
    stimulus_revision INT NOT NULL CHECK (stimulus_revision >= 0),
    turn_id UUID NOT NULL UNIQUE REFERENCES agent_turns(id),
    source_identity TEXT NOT NULL UNIQUE,
    completion_fingerprint TEXT NOT NULL
        CHECK (completion_fingerprint ~ '^[0-9a-f]{64}$'),
    result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, input_turn_id, stimulus_revision)
);

CREATE OR REPLACE FUNCTION complete_voice_turn(
    p_session_id UUID,
    p_input_turn_id UUID,
    p_input_order INT,
    p_stimulus_revision INT,
    p_turn_id UUID,
    p_user_text TEXT,
    p_assistant_text TEXT,
    p_context_version TEXT,
    p_playback_scope TEXT,
    p_completion_metadata JSONB,
    p_event_envelopes JSONB DEFAULT '[]'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_source_identity TEXT;
    v_fingerprint TEXT;
    v_receipt voice_turn_completions%ROWTYPE;
    v_turn agent_turns%ROWTYPE;
    v_chat_result JSONB;
    v_finish_result JSONB;
    v_result JSONB;
    v_existing_user public.chat_messages%ROWTYPE;
    v_existing_assistant public.chat_messages%ROWTYPE;
    v_event JSONB;
BEGIN
    IF p_session_id IS NULL OR p_input_turn_id IS NULL OR p_turn_id IS NULL THEN
        RAISE EXCEPTION 'voice completion identity is required';
    END IF;
    IF p_input_order IS NULL OR p_input_order < 0
       OR p_stimulus_revision IS NULL OR p_stimulus_revision < 0 THEN
        RAISE EXCEPTION 'voice completion order and revision must be non-negative';
    END IF;
    IF NULLIF(btrim(COALESCE(p_context_version, '')), '') IS NULL THEN
        RAISE EXCEPTION 'voice completion context_version is required';
    END IF;
    IF p_playback_scope NOT IN ('complete', 'partial', 'unplayed', 'unknown') THEN
        RAISE EXCEPTION 'voice playback scope is invalid';
    END IF;
    IF p_playback_scope <> 'complete' AND NULLIF(COALESCE(p_assistant_text, ''), '') IS NOT NULL THEN
        RAISE EXCEPTION 'only completely played output may be stored as assistant text';
    END IF;
    IF jsonb_typeof(COALESCE(p_completion_metadata, '{}'::jsonb)) <> 'object'
       OR jsonb_typeof(COALESCE(p_event_envelopes, '[]'::jsonb)) <> 'array' THEN
        RAISE EXCEPTION 'voice completion metadata or event envelopes have invalid shape';
    END IF;

    v_source_identity := 'voice:' || p_session_id::text || ':'
        || p_input_turn_id::text || ':' || p_stimulus_revision::text;
    v_fingerprint := encode(
        digest(
            convert_to(
                jsonb_build_object(
                    'session_id', p_session_id,
                    'input_turn_id', p_input_turn_id,
                    'input_order', p_input_order,
                    'stimulus_revision', p_stimulus_revision,
                    'turn_id', p_turn_id,
                    'user_text', COALESCE(p_user_text, ''),
                    'assistant_text', COALESCE(p_assistant_text, ''),
                    'context_version', p_context_version,
                    'playback_scope', p_playback_scope,
                    'completion_metadata', COALESCE(p_completion_metadata, '{}'::jsonb)
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );

    -- Match append_chat_message's per-session serialization boundary.
    PERFORM pg_advisory_xact_lock(hashtext(p_session_id::text));

    SELECT * INTO v_turn FROM agent_turns
    WHERE id = p_turn_id AND session_id = p_session_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'voice agent turn does not belong to the chat session';
    END IF;
    SELECT * INTO v_receipt FROM voice_turn_completions
    WHERE session_id = p_session_id
      AND input_turn_id = p_input_turn_id
      AND stimulus_revision = p_stimulus_revision
    FOR UPDATE;
    IF FOUND THEN
        IF v_receipt.completion_fingerprint <> v_fingerprint THEN
            RAISE EXCEPTION 'voice completion fingerprint conflicts with the stored receipt';
        END IF;
        RETURN v_receipt.result || jsonb_build_object('deduplicated', TRUE);
    END IF;
    IF v_turn.mode IS DISTINCT FROM 'voice'
       OR v_turn.user_message IS DISTINCT FROM COALESCE(p_user_text,'')
       OR v_turn.runtime_state->>'channel' IS DISTINCT FROM 'voice'
       OR v_turn.runtime_state->>'input_turn_id' IS DISTINCT FROM p_input_turn_id::text
       OR v_turn.runtime_state->>'input_order' IS DISTINCT FROM p_input_order::text
       OR v_turn.runtime_state->>'stimulus_revision' IS DISTINCT FROM p_stimulus_revision::text
       OR v_turn.runtime_state->>'context_version' IS DISTINCT FROM p_context_version
       OR v_turn.runtime_state->>'chat_source_identity' IS DISTINCT FROM v_source_identity THEN
        RETURN jsonb_build_object(
            'status','conflict',
            'source_identity',v_source_identity,
            'message_kind','turn'
        );
    END IF;
    IF v_turn.status NOT IN ('running', 'waiting_external') THEN
        RAISE EXCEPTION 'voice agent turn is already terminal';
    END IF;

    SELECT * INTO v_existing_user
    FROM public.chat_messages
    WHERE session_id=p_session_id
      AND source_message_id=v_source_identity || ':user'
    FOR UPDATE;
    IF FOUND AND (
        v_existing_user.role IS DISTINCT FROM 'user'
        OR v_existing_user.content IS DISTINCT FROM p_user_text
        OR v_existing_user.content_blocks IS DISTINCT FROM jsonb_build_array(
            jsonb_build_object('type','text','text',p_user_text)
        )
        OR v_existing_user.metadata->>'channel' IS DISTINCT FROM 'voice'
        OR v_existing_user.metadata->>'input_turn_id' IS DISTINCT FROM p_input_turn_id::text
        OR v_existing_user.metadata->>'input_order' IS DISTINCT FROM p_input_order::text
        OR v_existing_user.metadata->>'stimulus_revision' IS DISTINCT FROM p_stimulus_revision::text
        OR v_existing_user.metadata->>'context_version' IS DISTINCT FROM p_context_version
    ) THEN
        RETURN jsonb_build_object(
            'status','conflict',
            'source_identity',v_source_identity,
            'message_kind','user'
        );
    END IF;

    SELECT * INTO v_existing_assistant
    FROM public.chat_messages
    WHERE session_id=p_session_id
      AND source_message_id=v_source_identity || ':assistant'
    FOR UPDATE;
    IF FOUND AND (
        p_assistant_text IS NULL
        OR v_existing_assistant.role IS DISTINCT FROM 'assistant'
        OR v_existing_assistant.content IS DISTINCT FROM p_assistant_text
        OR v_existing_assistant.content_blocks IS DISTINCT FROM jsonb_build_array(
            jsonb_build_object('type','text','text',p_assistant_text)
        )
        OR v_existing_assistant.metadata->>'channel' IS DISTINCT FROM 'voice'
        OR v_existing_assistant.metadata->>'agent_turn_id' IS DISTINCT FROM p_turn_id::text
        OR v_existing_assistant.metadata->>'playback_scope' IS DISTINCT FROM p_playback_scope
    ) THEN
        RETURN jsonb_build_object(
            'status','conflict',
            'source_identity',v_source_identity,
            'message_kind','assistant'
        );
    END IF;

    v_chat_result := record_chat_session_turn(
        p_session_id,
        NULLIF(p_user_text, ''),
        NULLIF(p_assistant_text, ''),
        'web',
        jsonb_build_object(
            'channel', 'voice',
            'source_identity', v_source_identity,
            'user_source_message_id', v_source_identity || ':user',
            'assistant_source_message_id', v_source_identity || ':assistant',
            'user_metadata', jsonb_build_object(
                'channel', 'voice',
                'input_turn_id', p_input_turn_id,
                'input_order', p_input_order,
                'stimulus_revision', p_stimulus_revision,
                'context_version', p_context_version
            ),
            'assistant_metadata', COALESCE(p_completion_metadata, '{}'::jsonb)
                || jsonb_build_object(
                    'channel', 'voice',
                    'playback_scope', p_playback_scope,
                    'agent_turn_id', p_turn_id::text
                )
        )
    );
    v_finish_result := finish_agent_turn(
        p_turn_id,
        jsonb_build_object(
            'status', 'completed',
            'stopped_reason', 'voice_playback_' || p_playback_scope,
            'playback_scope', p_playback_scope,
            'completion_metadata', COALESCE(p_completion_metadata, '{}'::jsonb)
        )
    );

    v_result := jsonb_build_object(
        'status', 'completed',
        'session_id', p_session_id,
        'input_turn_id', p_input_turn_id,
        'input_order', p_input_order,
        'stimulus_revision', p_stimulus_revision,
        'turn_id', p_turn_id,
        'source_identity', v_source_identity,
        'playback_scope', p_playback_scope,
        'chat_result', v_chat_result,
        'finish_result', v_finish_result,
        'completion_metadata', COALESCE(p_completion_metadata, '{}'::jsonb),
        'event_envelopes', COALESCE(p_event_envelopes, '[]'::jsonb)
    );

    INSERT INTO voice_turn_completions (
        session_id, input_turn_id, input_order, stimulus_revision, turn_id,
        source_identity, completion_fingerprint, result
    ) VALUES (
        p_session_id, p_input_turn_id, p_input_order, p_stimulus_revision, p_turn_id,
        v_source_identity, v_fingerprint, v_result
    );

    FOR v_event IN SELECT value FROM jsonb_array_elements(COALESCE(p_event_envelopes, '[]'::jsonb))
    LOOP
        PERFORM record_behavior_event(
            (v_event->>'event_id')::uuid,
            COALESCE((v_event->>'schema_version')::int, 1),
            v_event->>'event_type',
            v_event->>'stream_id',
            p_session_id,
            v_event->>'request_id',
            p_stimulus_revision,
            p_input_turn_id::text,
            v_event->>'response_id',
            v_event->>'media_session_id',
            (v_event->>'output_epoch')::int,
            p_turn_id,
            v_event->>'task_id',
            v_event->>'call_id',
            (v_event->>'ts')::timestamptz,
            COALESCE(v_event->>'source', 'server'),
            COALESCE(v_event->>'producer_id', 'hexis.voice-realtime'),
            COALESCE((v_event->>'producer_seq')::bigint, 0),
            COALESCE(v_event->>'channel', 'voice'),
            COALESCE(v_event->'payload', '{}'::jsonb)
        );
    END LOOP;

    RETURN v_result || jsonb_build_object('deduplicated', FALSE);
END;
$$;
