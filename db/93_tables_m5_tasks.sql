-- Durable M5 action, task, and first-work identities.
SET search_path = public, ag_catalog, "$user";

CREATE TABLE IF NOT EXISTS public.m5_actions (
    command_id UUID PRIMARY KEY,
    origin_session_id UUID NOT NULL
        REFERENCES public.chat_sessions(id) ON DELETE RESTRICT,
    tool_name TEXT NOT NULL
        CHECK (tool_name IN ('submit', 'pend_message', 'remember', 'add_evidence')),
    argument_digest TEXT NOT NULL
        CHECK (argument_digest ~ '^[0-9a-f]{64}$'),
    "authorization" JSONB NOT NULL
        CHECK (jsonb_typeof("authorization") = 'object'),
    actual_call_id TEXT NOT NULL
        CHECK (NULLIF(btrim(actual_call_id), '') IS NOT NULL),
    state TEXT NOT NULL DEFAULT 'accepted'
        CHECK (state IN (
            'accepted', 'pending', 'selection_cancelled', 'done',
            'failed', 'unknown'
        )),
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    arguments JSONB,
    execution_started_at TIMESTAMPTZ,
    CONSTRAINT m5_actions_arguments_tool_check
        CHECK (arguments IS NULL OR tool_name IN ('remember', 'add_evidence')),
    CONSTRAINT m5_actions_execution_started_tool_check
        CHECK (execution_started_at IS NULL OR tool_name IN ('remember', 'add_evidence')),
    CONSTRAINT m5_actions_arguments_object_check
        CHECK (arguments IS NULL OR jsonb_typeof(arguments) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_m5_actions_accepted
    ON public.m5_actions (state, created_at)
    WHERE state = 'accepted';

CREATE TABLE IF NOT EXISTS public.m5_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_session_id UUID NOT NULL
        REFERENCES public.chat_sessions(id) ON DELETE RESTRICT,
    task_session_id UUID NOT NULL UNIQUE
        REFERENCES public.chat_sessions(id) ON DELETE RESTRICT,
    submit_command_id UUID NOT NULL UNIQUE
        REFERENCES public.m5_actions(command_id) ON DELETE RESTRICT,
    task_description TEXT NOT NULL
        CHECK (NULLIF(btrim(task_description), '') IS NOT NULL),
    original_user_request TEXT NOT NULL
        CHECK (NULLIF(btrim(original_user_request), '') IS NOT NULL),
    product_state TEXT NOT NULL DEFAULT 'working'
        CHECK (product_state IN ('working', 'idle')),
    current_work_execution_id UUID,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    next_receive_order BIGINT NOT NULL DEFAULT 0 CHECK (next_receive_order >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_m5_tasks_id_origin
    ON public.m5_tasks (id, origin_session_id);

CREATE INDEX IF NOT EXISTS idx_m5_tasks_origin_created
    ON public.m5_tasks (origin_session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_m5_tasks_state_work
    ON public.m5_tasks (product_state, current_work_execution_id);

CREATE TABLE IF NOT EXISTS public.m5_work_rounds (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL
        REFERENCES public.m5_tasks(id) ON DELETE RESTRICT,
    message_id UUID,
    status TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed', 'running', 'completed', 'failed', 'unknown')),
    turn_id UUID REFERENCES public.agent_turns(id) ON DELETE RESTRICT,
    input TEXT NOT NULL CHECK (NULLIF(btrim(input), '') IS NOT NULL),
    result JSONB,
    error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    UNIQUE (id, task_id)
);

-- S5 adds the append-only pending queue and its deferred bidirectional work links.
CREATE TABLE IF NOT EXISTS public.m5_pending_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL
        REFERENCES public.m5_tasks(id) ON DELETE RESTRICT,
    command_id UUID NOT NULL UNIQUE
        REFERENCES public.m5_actions(command_id) ON DELETE RESTRICT,
    receive_order BIGINT NOT NULL CHECK (receive_order > 0),
    message TEXT NOT NULL CHECK (NULLIF(btrim(message), '') IS NOT NULL),
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    claimed_work_execution_id UUID UNIQUE,
    UNIQUE (task_id, receive_order),
    UNIQUE (id, task_id)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'm5_work_rounds_message_id_key'
          AND conrelid = 'public.m5_work_rounds'::regclass
    ) THEN
        ALTER TABLE public.m5_work_rounds
            ADD CONSTRAINT m5_work_rounds_message_id_key UNIQUE (message_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'm5_work_rounds_message_task_fkey'
          AND conrelid = 'public.m5_work_rounds'::regclass
    ) THEN
        ALTER TABLE public.m5_work_rounds
            ADD CONSTRAINT m5_work_rounds_message_task_fkey
            FOREIGN KEY (message_id, task_id)
            REFERENCES public.m5_pending_messages(id, task_id)
            ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'm5_pending_claimed_work_task_fkey'
          AND conrelid = 'public.m5_pending_messages'::regclass
    ) THEN
        ALTER TABLE public.m5_pending_messages
            ADD CONSTRAINT m5_pending_claimed_work_task_fkey
            FOREIGN KEY (claimed_work_execution_id, task_id)
            REFERENCES public.m5_work_rounds(id, task_id)
            ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END;
$$;

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

-- S6 gives every certain, completed work round one immutable report identity.
CREATE TABLE IF NOT EXISTS public.m5_reports (
    report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL,
    origin_session_id UUID NOT NULL,
    work_execution_id UUID NOT NULL UNIQUE,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count BETWEEN 0 AND 3),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 16),
    rejected_signatures JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(rejected_signatures) = 'array'),
    outcome TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending', 'delivered', 'exhausted', 'refused')),
    summary TEXT,
    receiver_receipt JSONB,
    last_call_id TEXT,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT m5_reports_task_origin_fkey
        FOREIGN KEY (task_id, origin_session_id)
        REFERENCES public.m5_tasks(id, origin_session_id) ON DELETE RESTRICT,
    CONSTRAINT m5_reports_work_task_fkey
        FOREIGN KEY (work_execution_id, task_id)
        REFERENCES public.m5_work_rounds(id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_m5_reports_pending
    ON public.m5_reports (updated_at)
    WHERE outcome = 'pending';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'm5_tasks_current_work_execution_id_fkey'
          AND conrelid = 'public.m5_tasks'::regclass
    ) THEN
        ALTER TABLE public.m5_tasks
            ADD CONSTRAINT m5_tasks_current_work_execution_id_fkey
            FOREIGN KEY (current_work_execution_id)
            REFERENCES public.m5_work_rounds(id)
            ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END;
$$;
