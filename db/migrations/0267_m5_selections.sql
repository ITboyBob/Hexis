-- 0267_m5_selections.sql
-- Forward-only: selection rows reference immutable calls and are retained as task history.
SET search_path = public, ag_catalog, "$user";

-- S10 adds one durable, user-confirmed target for each pend_message action.
CREATE TABLE IF NOT EXISTS public.m5_selections (
    selection_id UUID PRIMARY KEY,
    command_id UUID NOT NULL UNIQUE
        REFERENCES public.m5_actions(command_id) ON DELETE RESTRICT,
    origin_session_id UUID NOT NULL
        REFERENCES public.chat_sessions(id) ON DELETE RESTRICT,
    actual_call_id TEXT NOT NULL
        CHECK (NULLIF(btrim(actual_call_id), '') IS NOT NULL),
    "authorization" JSONB NOT NULL
        CHECK (jsonb_typeof("authorization") = 'object'),
    message TEXT NOT NULL
        CHECK (NULLIF(btrim(message), '') IS NOT NULL),
    state TEXT NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'confirmed', 'cancelled', 'expired')),
    chosen_task_id UUID,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT m5_selections_chosen_task_origin_fkey
        FOREIGN KEY (chosen_task_id, origin_session_id)
        REFERENCES public.m5_tasks(id, origin_session_id) ON DELETE RESTRICT,
    CONSTRAINT m5_selections_chosen_task_state_check
        CHECK ((state = 'confirmed') = (chosen_task_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_m5_selections_origin_state
    ON public.m5_selections (origin_session_id, state)
    WHERE state = 'open';

CREATE INDEX IF NOT EXISTS idx_m5_work_task_started
    ON public.m5_work_rounds (task_id, started_at);

CREATE INDEX IF NOT EXISTS idx_m5_work_claimed
    ON public.m5_work_rounds (status)
    WHERE status = 'claimed';

CREATE INDEX IF NOT EXISTS idx_m5_pending_unclaimed
    ON public.m5_pending_messages (task_id, receive_order)
    WHERE claimed_work_execution_id IS NULL;

CREATE OR REPLACE FUNCTION public.append_m5_message(
    p_origin_session_id UUID,
    p_task_id UUID,
    p_command_id UUID,
    p_message TEXT,
    p_authorization JSONB
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_task public.m5_tasks%ROWTYPE;
    v_saved public.m5_actions%ROWTYPE;
    v_selection public.m5_selections%ROWTYPE;
    v_origin_status TEXT;
    v_actual_call_id TEXT;
    v_digest TEXT;
    v_order BIGINT;
    v_message_id UUID;
    v_inserted UUID;
    v_claim JSONB;
    v_result JSONB;
    v_selection_action BOOLEAN := FALSE;
BEGIN
    IF p_origin_session_id IS NULL OR p_task_id IS NULL OR p_command_id IS NULL
       OR NULLIF(btrim(COALESCE(p_message, '')), '') IS NULL
       OR jsonb_typeof(COALESCE(p_authorization, 'null'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'M5 pending message requires origin, task, command, message, and authorization';
    END IF;
    v_actual_call_id := NULLIF(btrim(p_authorization->>'actual_call_id'), '');
    v_digest := p_authorization->>'argument_digest';
    IF v_actual_call_id IS NULL
       OR p_authorization->>'origin_session_id' IS DISTINCT FROM p_origin_session_id::text
       OR p_authorization->>'task_id' IS DISTINCT FROM p_task_id::text
       OR p_authorization->>'command_id' IS DISTINCT FROM p_command_id::text
       OR v_digest IS NULL
       OR v_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'M5 pending authorization is incomplete or mismatched';
    END IF;

    SELECT * INTO v_task
    FROM public.m5_tasks
    WHERE id=p_task_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'accepted', false,
            'error', jsonb_build_object(
                'code', 'TARGET_NOT_FOUND',
                'message', 'The selected task no longer exists.',
                'retryable', false
            )
        );
    END IF;
    IF v_task.origin_session_id IS DISTINCT FROM p_origin_session_id THEN
        RETURN jsonb_build_object(
            'accepted', false,
            'error', jsonb_build_object(
                'code', 'TARGET_OWNERSHIP_MISMATCH',
                'message', 'The selected task does not belong to the source chat.',
                'retryable', false
            )
        );
    END IF;

    SELECT * INTO v_saved
    FROM public.m5_actions
    WHERE command_id=p_command_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_saved.origin_session_id IS DISTINCT FROM p_origin_session_id
           OR v_saved.tool_name IS DISTINCT FROM 'pend_message'
           OR v_saved.actual_call_id IS DISTINCT FROM v_actual_call_id THEN
            RETURN jsonb_build_object(
                'accepted', false,
                'error', jsonb_build_object(
                    'code', 'IDEMPOTENCY_CONFLICT',
                    'message', 'This command id was already used for different business arguments.',
                    'retryable', false
                )
            );
        END IF;

        IF v_saved.result IS NOT NULL THEN
            IF v_saved.argument_digest IS DISTINCT FROM v_digest
               OR v_saved.authorization->>'task_id'
                    IS DISTINCT FROM p_authorization->>'task_id' THEN
                RETURN jsonb_build_object(
                    'accepted', false,
                    'error', jsonb_build_object(
                        'code', 'IDEMPOTENCY_CONFLICT',
                        'message', 'This command id was already used for different business arguments.',
                        'retryable', false
                    )
                );
            END IF;
            RETURN v_saved.result || jsonb_build_object('replayed', true);
        END IF;

        SELECT * INTO v_selection
        FROM public.m5_selections
        WHERE command_id=p_command_id
        FOR UPDATE;
        IF v_saved.state = 'pending'
           AND FOUND
           AND v_selection.state = 'open'
           AND v_selection.origin_session_id = p_origin_session_id
           AND v_selection.actual_call_id = v_actual_call_id
           AND v_selection.message = p_message
           AND v_selection."authorization"->>'selection_argument_digest'
                = v_saved.argument_digest
           AND (v_selection."authorization" - 'selection_argument_digest')
                = (p_authorization - 'task_id' - 'command_id' - 'argument_digest') THEN
            UPDATE public.m5_actions
            SET argument_digest=v_digest,
                "authorization"=p_authorization,
                updated_at=clock_timestamp()
            WHERE command_id=p_command_id;
            v_selection_action := TRUE;
        ELSE
            RETURN jsonb_build_object(
                'accepted', false,
                'error', jsonb_build_object(
                    'code', 'EXECUTION_UNKNOWN',
                    'message', 'The original pending action has no stored business result.',
                    'retryable', false
                )
            );
        END IF;
    END IF;

    SELECT status INTO v_origin_status
    FROM public.chat_sessions
    WHERE id=p_origin_session_id
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
    IF v_task.product_state NOT IN ('working', 'idle') THEN
        RETURN jsonb_build_object(
            'accepted', false,
            'error', jsonb_build_object(
                'code', 'TARGET_NOT_FOUND',
                'message', 'The selected task is not available.',
                'retryable', false
            )
        );
    END IF;

    IF NOT v_selection_action THEN
        INSERT INTO public.m5_actions (
            command_id, origin_session_id, tool_name, argument_digest,
            "authorization", actual_call_id, state
        ) VALUES (
            p_command_id, p_origin_session_id, 'pend_message', v_digest,
            p_authorization, v_actual_call_id, 'pending'
        )
        ON CONFLICT (command_id) DO NOTHING
        RETURNING command_id INTO v_inserted;
        IF v_inserted IS NULL THEN
            RAISE EXCEPTION 'M5 pending command conflict changed while task lock was held: %', p_command_id;
        END IF;
    END IF;

    UPDATE public.m5_tasks
    SET next_receive_order=next_receive_order+1,
        updated_at=clock_timestamp()
    WHERE id=p_task_id
    RETURNING next_receive_order INTO v_order;
    INSERT INTO public.m5_pending_messages (
        task_id, command_id, receive_order, message
    ) VALUES (
        p_task_id, p_command_id, v_order, p_message
    ) RETURNING id INTO v_message_id;

    IF v_task.product_state = 'idle' THEN
        v_claim := public.claim_next_m5_message(p_task_id);
        IF v_claim IS NULL THEN
            RAISE EXCEPTION 'idle M5 task could not claim its persisted pending queue: %', p_task_id;
        END IF;
    END IF;
    v_result := jsonb_build_object(
        'accepted', true,
        'replayed', false,
        'operation_id', p_command_id,
        'state', 'pending',
        'task_id', p_task_id,
        'task_title', v_task.task_description,
        'message_id', v_message_id,
        'receive_order', v_order,
        'task_state', CASE WHEN v_claim IS NULL THEN v_task.product_state ELSE 'working' END,
        'work_execution_id', v_claim->'work_execution_id'
    );
    UPDATE public.m5_actions
    SET result=v_result, updated_at=clock_timestamp()
    WHERE command_id=p_command_id;
    RETURN v_result;
END;
$$;

-- S10 binds one persisted pend_message call to an origin-scoped selection.
CREATE OR REPLACE FUNCTION public.begin_m5_selection(
    p_selection_id UUID,
    p_command_id UUID,
    p_origin_session_id UUID,
    p_actual_call_id TEXT,
    p_authorization JSONB,
    p_message TEXT,
    p_argument_digest TEXT
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_origin_status TEXT;
    v_action public.m5_actions%ROWTYPE;
    v_selection public.m5_selections%ROWTYPE;
    v_inserted UUID;
    v_selection_authorization JSONB;
BEGIN
    IF p_selection_id IS NULL OR p_command_id IS NULL OR p_origin_session_id IS NULL
       OR NULLIF(btrim(COALESCE(p_actual_call_id,'')), '') IS NULL
       OR jsonb_typeof(COALESCE(p_authorization,'null'::jsonb)) <> 'object'
       OR NULLIF(btrim(COALESCE(p_message,'')), '') IS NULL
       OR p_argument_digest IS NULL OR p_argument_digest !~ '^[0-9a-f]{64}$'
       OR p_authorization->>'origin_session_id' IS DISTINCT FROM p_origin_session_id::text
       OR p_authorization->>'actual_call_id' IS DISTINCT FROM p_actual_call_id THEN
        RAISE EXCEPTION 'M5 selection requires a bound pend_message call and authorization';
    END IF;

    SELECT status INTO v_origin_status
    FROM public.chat_sessions
    WHERE id=p_origin_session_id
    FOR UPDATE;
    IF NOT FOUND OR v_origin_status <> 'active' THEN
        RETURN jsonb_build_object(
            'accepted',false,
            'error',jsonb_build_object(
                'code','ORIGIN_SESSION_UNAVAILABLE',
                'message','The source chat is unavailable.',
                'retryable',false
            )
        );
    END IF;

    SELECT * INTO v_selection
    FROM public.m5_selections
    WHERE command_id=p_command_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_selection.origin_session_id IS DISTINCT FROM p_origin_session_id
           OR v_selection.actual_call_id IS DISTINCT FROM p_actual_call_id
           OR v_selection.message IS DISTINCT FROM p_message
           OR v_selection.authorization->>'selection_argument_digest' IS DISTINCT FROM p_argument_digest THEN
            RETURN jsonb_build_object(
                'accepted',false,
                'error',jsonb_build_object(
                    'code','IDEMPOTENCY_CONFLICT',
                    'message','This command id was already used for different selection arguments.',
                    'retryable',false
                )
            );
        END IF;
        RETURN jsonb_build_object(
            'accepted',true,'replayed',true,
            'selection_id',v_selection.selection_id,
            'command_id',v_selection.command_id,
            'state',v_selection.state,
            'version',v_selection.version,
            'chosen_task_id',v_selection.chosen_task_id,
            'message',v_selection.message,
            'result',(SELECT result FROM public.m5_actions WHERE command_id=v_selection.command_id)
        );
    END IF;

    SELECT * INTO v_action
    FROM public.m5_actions
    WHERE command_id=p_command_id
    FOR UPDATE;
    IF FOUND THEN
        RETURN jsonb_build_object(
            'accepted',false,
            'error',jsonb_build_object(
                'code','IDEMPOTENCY_CONFLICT',
                'message','This command id already belongs to another M5 action.',
                'retryable',false
            )
        );
    END IF;

    INSERT INTO public.m5_actions (
        command_id,origin_session_id,tool_name,argument_digest,
        "authorization",actual_call_id,state
    ) VALUES (
        p_command_id,p_origin_session_id,'pend_message',p_argument_digest,
        p_authorization,p_actual_call_id,'pending'
    ) ON CONFLICT (command_id) DO NOTHING
    RETURNING command_id INTO v_inserted;
    IF v_inserted IS NULL THEN
        SELECT * INTO v_selection
        FROM public.m5_selections
        WHERE command_id=p_command_id
        FOR UPDATE;
        IF FOUND
           AND v_selection.origin_session_id=p_origin_session_id
           AND v_selection.actual_call_id=p_actual_call_id
           AND v_selection.message=p_message
           AND v_selection."authorization"->>'selection_argument_digest'=p_argument_digest THEN
            RETURN jsonb_build_object(
                'accepted',true,'replayed',true,
                'selection_id',v_selection.selection_id,
                'command_id',v_selection.command_id,
                'state',v_selection.state,
                'version',v_selection.version,
                'chosen_task_id',v_selection.chosen_task_id,
                'message',v_selection.message,
                'result',(SELECT result FROM public.m5_actions WHERE command_id=v_selection.command_id)
            );
        END IF;
        RETURN jsonb_build_object(
            'accepted',false,
            'error',jsonb_build_object(
                'code','IDEMPOTENCY_CONFLICT',
                'message','This command id was concurrently claimed.',
                'retryable',false
            )
        );
    END IF;

    v_selection_authorization := p_authorization || jsonb_build_object(
        'selection_argument_digest',p_argument_digest
    );
    INSERT INTO public.m5_selections (
        selection_id,command_id,origin_session_id,actual_call_id,
        "authorization",message
    ) VALUES (
        p_selection_id,p_command_id,p_origin_session_id,p_actual_call_id,
        v_selection_authorization,p_message
    );
    RETURN jsonb_build_object(
        'accepted',true,'replayed',false,
        'selection_id',p_selection_id,
        'command_id',p_command_id,
        'state','open','version',1,
        'message',p_message
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.close_m5_selection(
    p_selection_id UUID,
    p_origin_session_id UUID,
    p_version BIGINT,
    p_state TEXT,
    p_task_id UUID DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_selection public.m5_selections%ROWTYPE;
    v_origin_status TEXT;
    v_task_origin UUID;
    v_next_state TEXT;
    v_result JSONB;
BEGIN
    IF p_selection_id IS NULL OR p_origin_session_id IS NULL
       OR p_version IS NULL OR p_version <= 0
       OR p_state NOT IN ('confirmed','cancelled','expired') THEN
        RAISE EXCEPTION 'invalid M5 selection close request';
    END IF;
    SELECT * INTO v_selection
    FROM public.m5_selections
    WHERE selection_id=p_selection_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'accepted',false,
            'error',jsonb_build_object('code','SELECTION_NOT_FOUND','retryable',false)
        );
    END IF;
    IF v_selection.origin_session_id IS DISTINCT FROM p_origin_session_id THEN
        RETURN jsonb_build_object(
            'accepted',false,
            'error',jsonb_build_object('code','SELECTION_ORIGIN_MISMATCH','retryable',false)
        );
    END IF;
    IF v_selection.state <> 'open' THEN
        IF v_selection.state = p_state
           AND v_selection.chosen_task_id IS NOT DISTINCT FROM p_task_id THEN
            RETURN jsonb_build_object(
                'accepted',true,'replayed',true,
                'selection_id',v_selection.selection_id,
                'state',v_selection.state,
                'version',v_selection.version,
                'chosen_task_id',v_selection.chosen_task_id
            );
        END IF;
        RETURN jsonb_build_object(
            'accepted',false,
            'error',jsonb_build_object('code','SELECTION_ALREADY_CLOSED','retryable',false)
        );
    END IF;
    IF v_selection.version IS DISTINCT FROM p_version THEN
        RETURN jsonb_build_object(
            'accepted',false,
            'error',jsonb_build_object('code','SELECTION_VERSION_CONFLICT','retryable',false)
        );
    END IF;

    v_next_state := p_state;
    SELECT status INTO v_origin_status
    FROM public.chat_sessions
    WHERE id=p_origin_session_id
    FOR UPDATE;
    IF NOT FOUND OR v_origin_status <> 'active' THEN
        v_next_state := 'expired';
        p_task_id := NULL;
    END IF;
    IF v_next_state = 'confirmed' THEN
        IF p_task_id IS NULL THEN
            RETURN jsonb_build_object(
                'accepted',false,
                'error',jsonb_build_object('code','TARGET_NOT_FOUND','retryable',false)
            );
        END IF;
        SELECT origin_session_id INTO v_task_origin
        FROM public.m5_tasks
        WHERE id=p_task_id
          AND product_state IN ('working','idle')
        FOR UPDATE;
        IF NOT FOUND OR v_task_origin IS DISTINCT FROM p_origin_session_id THEN
            RETURN jsonb_build_object(
                'accepted',false,
                'error',jsonb_build_object('code','TARGET_OWNERSHIP_MISMATCH','retryable',false)
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.m5_pending_messages
            WHERE command_id=v_selection.command_id
              AND task_id=p_task_id
        ) OR NOT EXISTS (
            SELECT 1 FROM public.m5_actions
            WHERE command_id=v_selection.command_id
              AND state='pending'
              AND result IS NOT NULL
              AND "authorization"->>'task_id'=p_task_id::text
        ) THEN
            RAISE EXCEPTION 'confirmed M5 selection has no atomic pending message/action';
        END IF;
    END IF;

    IF v_next_state = 'cancelled' THEN
        v_result := jsonb_build_object(
            'accepted',false,'replayed',false,
            'operation_id',v_selection.command_id,
            'state','selection_cancelled',
            'selection_id',v_selection.selection_id
        );
        UPDATE public.m5_actions SET
            state='selection_cancelled',result=v_result,updated_at=clock_timestamp()
        WHERE command_id=v_selection.command_id AND state='pending' AND result IS NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'open M5 selection has no pending action';
        END IF;
    ELSIF v_next_state = 'expired' THEN
        v_result := jsonb_build_object(
            'accepted',false,'replayed',false,
            'operation_id',v_selection.command_id,
            'state','failed','selection_id',v_selection.selection_id,
            'error',jsonb_build_object(
                'code','SELECTION_EXPIRED',
                'message','The source chat or authorization is no longer active.',
                'retryable',false
            )
        );
        UPDATE public.m5_actions SET
            state='failed',result=v_result,updated_at=clock_timestamp()
        WHERE command_id=v_selection.command_id AND state='pending' AND result IS NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'expired M5 selection has no pending action';
        END IF;
    END IF;

    UPDATE public.m5_selections SET
        state=v_next_state,
        chosen_task_id=CASE WHEN v_next_state='confirmed' THEN p_task_id ELSE NULL END,
        version=version+1,
        updated_at=clock_timestamp()
    WHERE selection_id=p_selection_id;
    RETURN jsonb_build_object(
        'accepted',true,'replayed',false,
        'selection_id',v_selection.selection_id,
        'state',v_next_state,
        'version',v_selection.version+1,
        'chosen_task_id',CASE WHEN v_next_state='confirmed' THEN p_task_id ELSE NULL END,
        'command_id',v_selection.command_id
    );
END;
$$;
