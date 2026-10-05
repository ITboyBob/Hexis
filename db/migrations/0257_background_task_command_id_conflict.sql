-- Make background task command retries idempotent and conflicting retries fail loud.
SET search_path = public, ag_catalog, "$user";

CREATE OR REPLACE FUNCTION create_background_task(
    p_session_id UUID,
    p_command_id UUID,
    p_input JSONB
) RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    v_id UUID;
    v_existing public.background_tasks%ROWTYPE;
BEGIN
    INSERT INTO public.background_tasks (session_id, command_id, input)
    VALUES (p_session_id, p_command_id, p_input)
    ON CONFLICT (command_id) DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT * INTO v_existing FROM public.background_tasks
        WHERE command_id = p_command_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'background task insert conflict without existing row: %', p_command_id;
        END IF;
        IF v_existing.session_id IS DISTINCT FROM p_session_id
           OR v_existing.input IS DISTINCT FROM p_input THEN
            RAISE EXCEPTION 'background task command_id conflict: %', p_command_id;
        END IF;
        v_id := v_existing.id;
    END IF;

    RETURN v_id;
END;
$$;
