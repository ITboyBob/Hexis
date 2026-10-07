-- S7 report retry budget and terminal report closures.
-- Irreversible in place: function corrections require another forward migration.
SET search_path = public, ag_catalog, "$user";

CREATE OR REPLACE FUNCTION public.reserve_m5_report_attempt(
    p_report_id UUID,
    p_actual_call_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_report public.m5_reports%ROWTYPE;
BEGIN
    SELECT * INTO v_report FROM public.m5_reports
    WHERE report_id=p_report_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('allowed',false,'code','REPORT_NOT_FOUND');
    END IF;
    IF v_report.outcome <> 'pending' THEN
        RETURN jsonb_build_object('allowed',false,'code',upper(v_report.outcome));
    END IF;
    IF v_report.failure_count >= 3 THEN
        RETURN jsonb_build_object('allowed',false,'code','RETRY_EXHAUSTED');
    END IF;
    IF v_report.attempt_count >= 16 THEN
        RETURN jsonb_build_object('allowed',false,'code','TOOL_ATTEMPT_LIMIT');
    END IF;
    IF NULLIF(btrim(COALESCE(p_actual_call_id,'')),'') IS NULL THEN
        RETURN jsonb_build_object('allowed',false,'code','MISSING_CALL_ID');
    END IF;
    UPDATE public.m5_reports SET
        attempt_count=attempt_count+1,
        last_call_id=p_actual_call_id,
        updated_at=clock_timestamp()
    WHERE report_id=p_report_id;
    RETURN jsonb_build_object(
        'allowed',true,
        'attempt_count',v_report.attempt_count+1,
        'failure_count',v_report.failure_count
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.record_m5_report_failure(
    p_report_id UUID,
    p_code TEXT,
    p_error TEXT,
    p_actual_call_id TEXT DEFAULT NULL,
    p_rejected_signature TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_report public.m5_reports%ROWTYPE;
    v_repeated BOOLEAN := FALSE;
    v_failures INTEGER;
    v_outcome TEXT;
    v_signatures JSONB;
    v_round JSONB;
BEGIN
    SELECT * INTO v_report FROM public.m5_reports
    WHERE report_id=p_report_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'M5 report identity is unavailable';
    END IF;
    IF v_report.outcome <> 'pending' THEN
        RETURN jsonb_build_object(
            'outcome',v_report.outcome,'failure_count',v_report.failure_count,
            'attempt_count',v_report.attempt_count,'already_terminal',TRUE,
            'receiver_receipt',v_report.receiver_receipt
        );
    END IF;

    v_signatures := COALESCE(v_report.rejected_signatures,'[]'::jsonb);
    IF p_rejected_signature IS NOT NULL THEN
        v_repeated := v_signatures @> jsonb_build_array(p_rejected_signature);
        IF NOT v_repeated THEN
            v_signatures := v_signatures || jsonb_build_array(p_rejected_signature);
        END IF;
    END IF;
    v_failures := LEAST(3,v_report.failure_count+1);
    v_outcome := CASE
        WHEN v_repeated THEN 'refused'
        WHEN v_failures >= 3 OR v_report.attempt_count >= 16 THEN 'exhausted'
        ELSE 'pending'
    END;
    UPDATE public.m5_reports SET
        failure_count=v_failures,
        rejected_signatures=v_signatures,
        outcome=v_outcome,
        last_call_id=COALESCE(NULLIF(btrim(p_actual_call_id),''),last_call_id),
        last_error=jsonb_build_object('code',p_code,'message',p_error)::text,
        updated_at=clock_timestamp()
    WHERE report_id=p_report_id;

    IF v_outcome <> 'pending' THEN
        v_round := public.finish_m5_round(v_report.task_id,v_report.work_execution_id,v_outcome);
    END IF;
    RETURN jsonb_build_object(
        'outcome',v_outcome,'code',CASE WHEN v_repeated THEN 'REPEATED_REJECTION'
                                        WHEN v_outcome='exhausted' AND v_report.attempt_count >= 16
                                            THEN 'TOOL_ATTEMPT_LIMIT'
                                        WHEN v_outcome='exhausted' THEN 'RETRY_EXHAUSTED'
                                        ELSE p_code END,
        'failure_count',v_failures,'attempt_count',v_report.attempt_count,
        'repeated_signature',v_repeated,'round',v_round
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.finish_m5_report_terminal(
    p_report_id UUID,
    p_outcome TEXT,
    p_code TEXT,
    p_error TEXT,
    p_actual_call_id TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_report public.m5_reports%ROWTYPE;
    v_round JSONB;
BEGIN
    IF p_outcome NOT IN ('exhausted','refused') THEN
        RAISE EXCEPTION 'invalid M5 report terminal outcome: %',p_outcome;
    END IF;
    SELECT * INTO v_report FROM public.m5_reports
    WHERE report_id=p_report_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'M5 report identity is unavailable'; END IF;
    IF v_report.outcome='delivered' THEN
        RETURN jsonb_build_object('outcome','delivered','receiver_receipt',v_report.receiver_receipt);
    END IF;
    IF v_report.outcome IN ('exhausted','refused') THEN
        RETURN jsonb_build_object('outcome',v_report.outcome,'already_terminal',TRUE);
    END IF;
    UPDATE public.m5_reports SET
        outcome=p_outcome,
        last_call_id=COALESCE(NULLIF(btrim(p_actual_call_id),''),last_call_id),
        last_error=jsonb_build_object('code',p_code,'message',p_error)::text,
        updated_at=clock_timestamp()
    WHERE report_id=p_report_id;
    v_round := public.finish_m5_round(v_report.task_id,v_report.work_execution_id,p_outcome);
    RETURN jsonb_build_object('outcome',p_outcome,'code',p_code,'round',v_round);
END;
$$;
