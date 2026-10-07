-- S8 durable lightweight memory command arguments and one-shot claim lifecycle.
-- Forward-only schema expansion; action history is preserved.
SET search_path = public, ag_catalog, "$user";

ALTER TABLE public.m5_actions
    ADD COLUMN IF NOT EXISTS arguments JSONB,
    ADD COLUMN IF NOT EXISTS execution_started_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='m5_actions_arguments_tool_check' AND conrelid='public.m5_actions'::regclass) THEN
        ALTER TABLE public.m5_actions ADD CONSTRAINT m5_actions_arguments_tool_check
            CHECK (arguments IS NULL OR tool_name IN ('remember','add_evidence'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='m5_actions_execution_started_tool_check' AND conrelid='public.m5_actions'::regclass) THEN
        ALTER TABLE public.m5_actions ADD CONSTRAINT m5_actions_execution_started_tool_check
            CHECK (execution_started_at IS NULL OR tool_name IN ('remember','add_evidence'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='m5_actions_arguments_object_check' AND conrelid='public.m5_actions'::regclass) THEN
        ALTER TABLE public.m5_actions ADD CONSTRAINT m5_actions_arguments_object_check
            CHECK (arguments IS NULL OR jsonb_typeof(arguments)='object');
    END IF;
END;
$$;

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
