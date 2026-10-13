-- WebRTC：用户输入只保存云端身份与元数据，助手文字不代表已完整播放。
-- 前向增量；不改动已有文字聊天和历史语音完成函数，不删除既有数据。
SET search_path = public, ag_catalog, "$user";
ALTER TABLE voice_realtime_sessions ADD COLUMN IF NOT EXISTS rtc_owner_id UUID;
ALTER TABLE voice_realtime_sessions ADD COLUMN IF NOT EXISTS rtc_lease_expires_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS voice_rtc_inputs (
    input_turn_id UUID PRIMARY KEY,
    media_session_id UUID NOT NULL REFERENCES voice_realtime_sessions(id),
    session_id UUID NOT NULL REFERENCES chat_sessions(id),
    provider_item_id TEXT NOT NULL CHECK (length(provider_item_id) BETWEEN 1 AND 4096),
    input_order INT NOT NULL CHECK (input_order >= 0),
    turn_id UUID NOT NULL UNIQUE REFERENCES agent_turns(id),
    context_version TEXT NOT NULL CHECK (context_version ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{"input_modality":"audio","transcription_enabled":false}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (media_session_id, provider_item_id),
    UNIQUE (media_session_id, input_order)
);

CREATE TABLE IF NOT EXISTS voice_rtc_outputs (
    media_session_id UUID NOT NULL REFERENCES voice_realtime_sessions(id),
    response_id TEXT NOT NULL,
    input_turn_id UUID NOT NULL REFERENCES voice_rtc_inputs(input_turn_id),
    generation_status TEXT NOT NULL,
    playback_scope TEXT NOT NULL DEFAULT 'unknown' CHECK (playback_scope = 'unknown'),
    fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    message_id UUID REFERENCES chat_messages(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (media_session_id, response_id)
);

CREATE OR REPLACE FUNCTION start_rtc_voice_input(
    p_session UUID, p_media UUID, p_input UUID, p_order INT,
    p_item TEXT, p_context TEXT
) RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE
    v_saved voice_rtc_inputs%ROWTYPE;
    v_turn UUID;
    v_runtime JSONB;
BEGIN
    IF p_input IS NULL OR p_order IS NULL OR p_order < 0
       OR NULLIF(btrim(p_item),'') IS NULL OR p_context !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'RTC input metadata is invalid';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext(p_session::text));
    IF NOT EXISTS (
        SELECT 1 FROM voice_realtime_sessions r JOIN chat_sessions c ON c.id=r.session_id
        WHERE r.id=p_media AND r.session_id=p_session AND r.status='active' AND c.status='active'
    ) THEN
        RAISE EXCEPTION 'RTC input does not belong to an active selected session';
    END IF;
    SELECT * INTO v_saved FROM voice_rtc_inputs WHERE media_session_id=p_media AND provider_item_id=p_item;
    IF FOUND THEN
        IF v_saved.input_turn_id IS DISTINCT FROM p_input OR v_saved.input_order IS DISTINCT FROM p_order
           OR v_saved.context_version IS DISTINCT FROM p_context OR v_saved.session_id IS DISTINCT FROM p_session THEN
            RAISE EXCEPTION 'RTC input identity conflicts with its stored metadata';
        END IF;
        RETURN v_saved.turn_id;
    END IF;
    v_runtime := jsonb_build_object(
        'channel','voice','transport','webrtc','input_modality','audio',
        'input_turn_id',p_input,'input_order',p_order,'stimulus_revision',0,
        'context_version',p_context,'media_session_id',p_media,'provider_item_id',p_item,
        'chat_message_projection',true,
        'chat_source_identity',format('voice:%s:%s:0',p_session,p_input),
        'iterations',0,'energy_spent',0,'last_text','','tool_calls_made','[]'::jsonb
    );
    -- RTC 没有用户文字消息；由已提交的音频元数据建立真实 AgentLoop turn。
    INSERT INTO agent_turns(mode,session_id,user_message,runtime_state,phase,status)
    VALUES('voice',p_session,NULL,v_runtime,'execute','running') RETURNING id INTO v_turn;
    INSERT INTO voice_rtc_inputs(input_turn_id,media_session_id,session_id,provider_item_id,input_order,turn_id,context_version)
    VALUES(p_input,p_media,p_session,p_item,p_order,v_turn,p_context);
    PERFORM mark_user_contact();
    RETURN v_turn;
END;
$$;

CREATE OR REPLACE FUNCTION record_rtc_voice_output(
    p_session UUID, p_media UUID, p_input UUID, p_turn UUID, p_response TEXT,
    p_text TEXT, p_status TEXT, p_context TEXT, p_final BOOLEAN
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    v_input voice_rtc_inputs%ROWTYPE;
    v_saved voice_rtc_outputs%ROWTYPE;
    v_fingerprint TEXT;
    v_message UUID;
    v_projection JSONB;
    v_metadata JSONB;
BEGIN
    IF NULLIF(btrim(p_response),'') IS NULL OR NULLIF(btrim(p_status),'') IS NULL OR p_final IS NULL THEN
        RAISE EXCEPTION 'RTC output identity and generation status are required';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext(p_session::text));
    SELECT * INTO v_input FROM voice_rtc_inputs WHERE input_turn_id=p_input FOR UPDATE;
    IF NOT FOUND OR v_input.session_id IS DISTINCT FROM p_session
       OR v_input.media_session_id IS DISTINCT FROM p_media OR v_input.turn_id IS DISTINCT FROM p_turn
       OR v_input.context_version IS DISTINCT FROM p_context THEN
        RAISE EXCEPTION 'RTC output has no matching persisted audio input';
    END IF;
    v_fingerprint := encode(digest(convert_to(jsonb_build_object(
        'input',p_input,'turn',p_turn,'response',p_response,'text',COALESCE(p_text,''),
        'status',p_status,'context',p_context,'final',p_final
    )::text,'UTF8'),'sha256'),'hex');
    SELECT * INTO v_saved FROM voice_rtc_outputs WHERE media_session_id=p_media AND response_id=p_response;
    IF FOUND THEN
        IF v_saved.fingerprint IS DISTINCT FROM v_fingerprint THEN
            RAISE EXCEPTION 'RTC output conflicts with its recorded generation';
        END IF;
        RETURN jsonb_build_object('turn_id',p_turn,'message_id',v_saved.message_id,'replayed',true);
    END IF;
    v_metadata := jsonb_build_object(
        'channel','voice','transport','webrtc','input_turn_id',p_input,'turn_id',p_turn,
        'agent_turn_id',p_turn,'response_id',p_response,'media_session_id',p_media,
        'context_version',p_context,'generation_status',p_status,
        'content_source','model_generation','playback_scope','unknown'
    );
    IF NULLIF(btrim(COALESCE(p_text,'')),'') IS NOT NULL THEN
        v_projection := append_chat_message_blocks(p_session,'assistant',p_text,
            jsonb_build_array(jsonb_build_object('type','text','text',p_text)),v_metadata,
            format('rtc:%s:%s:assistant',p_media,p_response),true);
        v_message := COALESCE(v_projection->>'message_id',v_projection->>'id')::uuid;
    END IF;
    INSERT INTO voice_rtc_outputs(media_session_id,response_id,input_turn_id,generation_status,fingerprint,message_id)
    VALUES(p_media,p_response,p_input,p_status,v_fingerprint,v_message);
    IF p_final THEN
        UPDATE agent_turns SET
            status=CASE WHEN p_status='completed' THEN 'completed'
                        WHEN p_status IN ('cancelled','canceled') THEN 'cancelled' ELSE 'failed' END,
            stopped_reason='rtc_generation_' || p_status,
            runtime_state=runtime_state || jsonb_build_object('playback_scope','unknown','generation_status',p_status),
            result=jsonb_build_object('text_generated',COALESCE(p_text,''),'content_source','model_generation',
                'generation_status',p_status,'playback_scope','unknown'),
            completed_at=clock_timestamp(),updated_at=clock_timestamp()
        -- 迟到的生成事实可以保存，但不得覆写关闭或租约回收形成的终态。
        WHERE id=p_turn AND status IN ('running','waiting_external');
    END IF;
    RETURN jsonb_build_object('turn_id',p_turn,'message_id',v_message,'replayed',false);
END;
$$;
