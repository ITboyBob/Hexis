-- Expand the immutable tool-surface audit without rewriting historical rows.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';

DO $$
DECLARE
    constraint_name TEXT;
    constraint_definition TEXT;
    allowed_values TEXT[];
    constraint_count INT;
BEGIN
    IF to_regclass('public.tool_surface_decision_events') IS NULL THEN
        RAISE EXCEPTION 'public.tool_surface_decision_events is missing';
    END IF;

    SELECT count(*) INTO constraint_count
    FROM pg_constraint c
    JOIN pg_attribute a
      ON a.attrelid = c.conrelid
     AND a.attnum = ANY(c.conkey)
    WHERE c.conrelid = 'public.tool_surface_decision_events'::regclass
      AND c.contype = 'c'
      AND a.attname = 'decision_kind';

    IF constraint_count <> 1 THEN
        RAISE EXCEPTION
            'expected one decision_kind CHECK constraint, found %', constraint_count;
    END IF;

    SELECT c.conname, pg_get_constraintdef(c.oid)
    INTO constraint_name, constraint_definition
    FROM pg_constraint c
    JOIN pg_attribute a
      ON a.attrelid = c.conrelid
     AND a.attnum = ANY(c.conkey)
    WHERE c.conrelid = 'public.tool_surface_decision_events'::regclass
      AND c.contype = 'c'
      AND a.attname = 'decision_kind';

    SELECT array_agg(m[1] ORDER BY m[1])
    INTO allowed_values
    FROM regexp_matches(constraint_definition, $regex$'([^']*)'$regex$, 'g') AS m;

    IF cardinality(allowed_values) = 3
       AND allowed_values @> ARRAY['background_default', 'selection', 'skill_activation'] THEN
        NULL;
    ELSIF cardinality(allowed_values) = 2
       AND allowed_values @> ARRAY['selection', 'skill_activation'] THEN
        EXECUTE format(
            'ALTER TABLE public.tool_surface_decision_events DROP CONSTRAINT %I',
            constraint_name
        );
        ALTER TABLE public.tool_surface_decision_events
            ADD CONSTRAINT tool_surface_decision_events_decision_kind_check
            CHECK (decision_kind IN ('selection', 'skill_activation', 'background_default'))
            NOT VALID;
    ELSE
        RAISE EXCEPTION 'unexpected decision_kind CHECK definition: %', constraint_definition;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.record_tool_surface_decision(
    p_session_id UUID,
    p_surface TEXT,
    p_tool_context TEXT,
    p_decision_kind TEXT,
    p_input_text_hash TEXT,
    p_selected_skills TEXT[],
    p_considered JSONB,
    p_allowed_tools TEXT[],
    p_reachable_tools TEXT[],
    p_unreachable_tools TEXT[],
    p_available_skill_count INT,
    p_registry_kind TEXT
) RETURNS UUID AS $$
DECLARE
    new_id UUID;
BEGIN
    INSERT INTO public.tool_surface_decision_events (
        session_id, surface, tool_context, decision_kind, input_text_hash, selected_skills,
        considered, allowed_tools, reachable_tools, unreachable_tools,
        available_skill_count, registry_kind
    ) VALUES (
        p_session_id,
        COALESCE(NULLIF(btrim(p_surface), ''), 'chat'),
        COALESCE(NULLIF(btrim(p_tool_context), ''), 'chat'),
        CASE
            WHEN p_decision_kind IN ('selection', 'skill_activation', 'background_default')
                THEN p_decision_kind
            ELSE 'selection'
        END,
        COALESCE(NULLIF(btrim(p_input_text_hash), ''), repeat('0', 64)),
        COALESCE(p_selected_skills, ARRAY[]::TEXT[]),
        COALESCE(p_considered, '[]'::jsonb),
        COALESCE(p_allowed_tools, ARRAY[]::TEXT[]),
        COALESCE(p_reachable_tools, ARRAY[]::TEXT[]),
        COALESCE(p_unreachable_tools, ARRAY[]::TEXT[]),
        GREATEST(COALESCE(p_available_skill_count, 0), 0),
        COALESCE(NULLIF(btrim(p_registry_kind), ''), 'default')
    ) RETURNING id INTO new_id;
    RETURN new_id;
END;
$$ LANGUAGE plpgsql;
