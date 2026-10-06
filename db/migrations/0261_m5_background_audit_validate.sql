-- Validate the expanded audit constraint in a separate, bounded transaction.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';

DO $$
DECLARE
    constraint_name TEXT;
    constraint_definition TEXT;
    allowed_values TEXT[];
    is_validated BOOLEAN;
BEGIN
    SELECT c.conname, pg_get_constraintdef(c.oid), c.convalidated
    INTO constraint_name, constraint_definition, is_validated
    FROM pg_constraint c
    JOIN pg_attribute a
      ON a.attrelid = c.conrelid
     AND a.attnum = ANY(c.conkey)
    WHERE c.conrelid = to_regclass('public.tool_surface_decision_events')
      AND c.contype = 'c'
      AND a.attname = 'decision_kind';

    IF constraint_name IS NULL THEN
        RAISE EXCEPTION 'decision_kind CHECK constraint is missing';
    END IF;

    SELECT array_agg(m[1] ORDER BY m[1])
    INTO allowed_values
    FROM regexp_matches(constraint_definition, $regex$'([^']*)'$regex$, 'g') AS m;

    IF cardinality(allowed_values) <> 3
       OR NOT (allowed_values @> ARRAY['background_default', 'selection', 'skill_activation']) THEN
        RAISE EXCEPTION 'unexpected decision_kind CHECK definition: %', constraint_definition;
    END IF;

    IF NOT is_validated THEN
        EXECUTE format(
            'ALTER TABLE public.tool_surface_decision_events VALIDATE CONSTRAINT %I',
            constraint_name
        );
    END IF;
END;
$$;
