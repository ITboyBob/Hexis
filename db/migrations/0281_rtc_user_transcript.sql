-- RTC 最终转录：沿用真实音频输入身份，幂等保存并维护聊天顺序。
-- 仅增加函数；旧运行时的默认转录元数据不变，不回填历史记录。
SET search_path = public, ag_catalog, "$user";

CREATE OR REPLACE FUNCTION save_rtc_user_transcript(
    p_session UUID, p_media UUID, p_input UUID, p_turn UUID, p_text TEXT
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    v_input voice_rtc_inputs%ROWTYPE;
    v_saved chat_messages%ROWTYPE;
    v_projection JSONB;
    v_message UUID;
    v_source TEXT;
    v_anchor INT;
    v_last INT;
    v_row RECORD;
BEGIN
    IF NULLIF(btrim(p_text),'') IS NULL OR length(p_text)>32768 THEN
        RAISE EXCEPTION 'RTC transcript must be nonempty and bounded';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext(p_session::text));
    SELECT * INTO v_input FROM voice_rtc_inputs WHERE input_turn_id=p_input FOR UPDATE;
    IF NOT FOUND OR v_input.session_id IS DISTINCT FROM p_session
       OR v_input.media_session_id IS DISTINCT FROM p_media OR v_input.turn_id IS DISTINCT FROM p_turn THEN
        RAISE EXCEPTION 'RTC transcript has no matching audio input';
    END IF;
    v_source := format('voice:%s:%s:0:user',p_session,p_input);
    SELECT * INTO v_saved FROM chat_messages WHERE session_id=p_session AND source_message_id=v_source;
    IF FOUND THEN
        IF v_saved.role IS DISTINCT FROM 'user' OR v_saved.content IS DISTINCT FROM p_text
           OR v_saved.metadata->>'agent_turn_id' IS DISTINCT FROM p_turn::text THEN
            RAISE EXCEPTION 'RTC transcript conflicts with its saved message';
        END IF;
        RETURN jsonb_build_object('message_id',v_saved.id,'replayed',true);
    END IF;
    -- 为新消息寻找本轮或后续音频轮次的最早消息，不改变旧消息的相对顺序。
    SELECT min(m.ordinal) INTO v_anchor FROM chat_messages m
    JOIN voice_rtc_inputs i ON i.turn_id::text=m.metadata->>'agent_turn_id'
    WHERE m.session_id=p_session AND i.media_session_id=p_media AND i.input_order>=v_input.input_order;
    v_projection := append_chat_message_blocks(p_session,'user',p_text,
        jsonb_build_array(jsonb_build_object('type','text','text',p_text)),
        jsonb_build_object('channel','voice','transport','webrtc','input_turn_id',p_input,
            'input_order',v_input.input_order,'agent_turn_id',p_turn,'media_session_id',p_media,
            'content_source','asr_reference'),v_source,true);
    v_message := COALESCE(v_projection->>'message_id',v_projection->>'id')::uuid;
    IF v_anchor IS NOT NULL THEN
        SELECT ordinal INTO v_last FROM chat_messages WHERE id=v_message;
        -- 非延迟唯一约束下先移入空闲位，再倒序移动，事务外不可见中间状态。
        UPDATE chat_messages SET ordinal=v_last+1 WHERE id=v_message;
        FOR v_row IN SELECT id,ordinal FROM chat_messages
            WHERE session_id=p_session AND ordinal>=v_anchor AND ordinal<v_last ORDER BY ordinal DESC LOOP
            UPDATE chat_messages SET ordinal=v_row.ordinal+1 WHERE id=v_row.id;
        END LOOP;
        UPDATE chat_messages SET ordinal=v_anchor WHERE id=v_message;
    END IF;
    RETURN jsonb_build_object('message_id',v_message,'replayed',false);
END;
$$;

-- 新运行时保存助手文字时补全输入顺序，旧函数和旧运行时保持兼容。
CREATE OR REPLACE FUNCTION record_rtc_voice_output_with_input_order(
    p_session UUID, p_media UUID, p_input UUID, p_turn UUID, p_response TEXT,
    p_text TEXT, p_status TEXT, p_context TEXT, p_final BOOLEAN
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    v_result JSONB;
BEGIN
    v_result := record_rtc_voice_output(p_session,p_media,p_input,p_turn,p_response,
        p_text,p_status,p_context,p_final);
    UPDATE chat_messages m SET metadata=m.metadata || jsonb_build_object('input_order',i.input_order)
    FROM voice_rtc_inputs i WHERE m.id=(v_result->>'message_id')::uuid AND i.input_turn_id=p_input;
    RETURN v_result;
END;
$$;
