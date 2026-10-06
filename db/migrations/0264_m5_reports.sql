-- S6: persist a report identity for each completed M5 work execution.
SET search_path = public, ag_catalog, "$user";

CREATE UNIQUE INDEX IF NOT EXISTS idx_m5_tasks_id_origin
    ON public.m5_tasks (id, origin_session_id);

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

CREATE OR REPLACE FUNCTION public.finish_m5_work_round(
    p_task_id UUID,
    p_work_execution_id UUID,
    p_status TEXT,
    p_result JSONB,
    p_error TEXT,
    p_turn_id UUID DEFAULT NULL
) RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
DECLARE
    v_task public.m5_tasks%ROWTYPE;
    v_work public.m5_work_rounds%ROWTYPE;
    v_contexts JSONB;
BEGIN
    IF p_status NOT IN ('completed', 'failed', 'unknown') THEN
        RAISE EXCEPTION 'invalid M5 work terminal status: %', p_status;
    END IF;
    SELECT * INTO v_task FROM public.m5_tasks
    WHERE id=p_task_id FOR UPDATE;
    IF NOT FOUND OR v_task.product_state <> 'working'
       OR v_task.current_work_execution_id IS DISTINCT FROM p_work_execution_id THEN
        RETURN FALSE;
    END IF;
    SELECT * INTO v_work FROM public.m5_work_rounds
    WHERE id=p_work_execution_id AND task_id=p_task_id FOR UPDATE;
    IF NOT FOUND OR v_work.status <> 'running' THEN
        RETURN FALSE;
    END IF;

    UPDATE public.m5_work_rounds
    SET status=p_status,
        turn_id=p_turn_id,
        result=CASE WHEN p_status='completed' THEN COALESCE(p_result,'{}'::jsonb) ELSE NULL END,
        error=CASE WHEN p_status='completed' THEN NULL ELSE p_error END,
        finished_at=clock_timestamp()
    WHERE id=p_work_execution_id AND task_id=p_task_id;
    INSERT INTO public.m5_reports(task_id,origin_session_id,work_execution_id)
    VALUES (p_task_id,v_task.origin_session_id,p_work_execution_id)
    ON CONFLICT (work_execution_id) DO NOTHING;

    UPDATE public.m5_tasks SET revision=revision+1,updated_at=clock_timestamp()
    WHERE id=p_task_id;
    SELECT COALESCE(metadata->'m5_work_contexts','{}'::jsonb)
    INTO v_contexts FROM public.chat_sessions
    WHERE id=v_task.task_session_id FOR UPDATE;
    v_contexts := v_contexts - p_work_execution_id::text;
    IF v_contexts='{}'::jsonb THEN
        UPDATE public.chat_sessions
        SET metadata=COALESCE(metadata,'{}'::jsonb)-'m5_work_contexts',updated_at=clock_timestamp()
        WHERE id=v_task.task_session_id;
    ELSE
        UPDATE public.chat_sessions
        SET metadata=jsonb_set(COALESCE(metadata,'{}'::jsonb),'{m5_work_contexts}',v_contexts,TRUE),
            updated_at=clock_timestamp()
        WHERE id=v_task.task_session_id;
    END IF;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION public.deliver_m5_report(
    p_report_id UUID,
    p_bound_origin UUID,
    p_summary TEXT,
    p_actual_call_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_report public.m5_reports%ROWTYPE;
    v_task public.m5_tasks%ROWTYPE;
    v_work public.m5_work_rounds%ROWTYPE;
    v_origin_status TEXT;
    v_message JSONB;
    v_round JSONB;
    v_receipt JSONB;
BEGIN
    IF p_report_id IS NULL OR p_bound_origin IS NULL
       OR NULLIF(btrim(COALESCE(p_actual_call_id,'')),'') IS NULL
       OR NULLIF(btrim(COALESCE(p_summary,'')),'') IS NULL THEN
        RAISE EXCEPTION 'M5 report delivery requires bound identity and summary';
    END IF;
    SELECT * INTO v_report FROM public.m5_reports
    WHERE report_id=p_report_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'M5 report identity is unavailable'; END IF;
    IF v_report.origin_session_id IS DISTINCT FROM p_bound_origin THEN
        RAISE EXCEPTION 'M5 report origin does not match the host binding';
    END IF;
    IF v_report.outcome='delivered' THEN RETURN v_report.receiver_receipt; END IF;
    IF v_report.outcome <> 'pending' THEN
        RAISE EXCEPTION 'M5 report is already terminal: %',v_report.outcome;
    END IF;

    SELECT * INTO v_task FROM public.m5_tasks
    WHERE id=v_report.task_id AND origin_session_id=p_bound_origin FOR UPDATE;
    IF NOT FOUND OR v_task.current_work_execution_id IS DISTINCT FROM v_report.work_execution_id
       OR v_task.product_state <> 'working' THEN
        RAISE EXCEPTION 'M5 report task binding is no longer active';
    END IF;
    SELECT * INTO v_work FROM public.m5_work_rounds
    WHERE id=v_report.work_execution_id AND task_id=v_report.task_id FOR UPDATE;
    IF NOT FOUND OR v_work.status NOT IN ('completed','failed','unknown') THEN
        RAISE EXCEPTION 'M5 report work has not ended';
    END IF;
    SELECT status INTO v_origin_status FROM public.chat_sessions
    WHERE id=p_bound_origin FOR UPDATE;
    IF NOT FOUND OR v_origin_status <> 'active' THEN
        RAISE EXCEPTION 'M5 report origin session is unavailable';
    END IF;

    v_message := public.append_chat_message(
        p_bound_origin,'assistant',p_summary,
        jsonb_build_object('message_kind','m5_report','report_id',p_report_id,
                           'task_id',v_report.task_id,'work_execution_id',v_report.work_execution_id,
                           'actual_call_id',p_actual_call_id),
        'm5-report:'||p_report_id::text,TRUE
    );
    v_receipt := jsonb_build_object(
        'outcome','delivered','report_id',p_report_id,'operation_id',p_report_id,
        'task_id',v_report.task_id,
        'origin_session_id',p_bound_origin,'message_id',v_message->'message_id',
        'actual_call_id',p_actual_call_id
    );
    UPDATE public.m5_reports SET outcome='delivered',summary=p_summary,last_call_id=p_actual_call_id,
        receiver_receipt=v_receipt,updated_at=clock_timestamp()
    WHERE report_id=p_report_id;
    v_round := public.finish_m5_round(v_report.task_id,v_report.work_execution_id,'delivered');
    v_receipt := v_receipt || jsonb_build_object('round',v_round);
    UPDATE public.m5_reports SET receiver_receipt=v_receipt WHERE report_id=p_report_id;
    RETURN v_receipt;
END;
$$;
