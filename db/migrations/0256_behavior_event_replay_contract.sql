-- Make behavior event replay pagination explicit and stream-scoped.
SET search_path = public, ag_catalog, "$user";

ALTER TABLE behavior_events
    ALTER COLUMN recorded_at SET DEFAULT clock_timestamp();

DROP FUNCTION IF EXISTS get_behavior_events(
    UUID, TEXT, TEXT, UUID, TEXT, TEXT, TEXT, TEXT, INT, INT
);
DROP FUNCTION IF EXISTS replay_behavior_events(UUID, TEXT, INT);

CREATE OR REPLACE FUNCTION get_behavior_events(
    p_session_id UUID DEFAULT NULL,
    p_stream_id TEXT DEFAULT NULL,
    p_event_type TEXT DEFAULT NULL,
    p_turn_id UUID DEFAULT NULL,
    p_request_id TEXT DEFAULT NULL,
    p_response_id TEXT DEFAULT NULL,
    p_task_id TEXT DEFAULT NULL,
    p_call_id TEXT DEFAULT NULL,
    p_limit INT DEFAULT 100,
    p_offset INT DEFAULT 0,
    p_cursor JSONB DEFAULT NULL
) RETURNS JSONB
LANGUAGE sql STABLE
AS $$
    WITH scope AS (
        SELECT e.*
        FROM behavior_events e
        WHERE (p_stream_id IS NULL OR e.stream_id = p_stream_id)
          AND (
                p_cursor IS NULL
                OR e.stream_id IN (SELECT key::text FROM jsonb_each_text(p_cursor))
              )
          AND (
                p_cursor IS NULL
                OR e.seq > (
                    SELECT (value)::bigint
                    FROM jsonb_each_text(p_cursor)
                    WHERE key = e.stream_id
                )
              )
    ),
    filtered AS (
        SELECT * FROM scope
        WHERE (p_session_id IS NULL OR session_id = p_session_id)
          AND (p_event_type IS NULL OR event_type = p_event_type)
          AND (p_turn_id IS NULL OR turn_id = p_turn_id)
          AND (p_request_id IS NULL OR request_id = p_request_id)
          AND (p_response_id IS NULL OR response_id = p_response_id)
          AND (p_task_id IS NULL OR task_id = p_task_id)
          AND (p_call_id IS NULL OR call_id = p_call_id)
    ),
    ordered AS (
        SELECT filtered.*,
               row_number() OVER (ORDER BY stream_id, seq) - 1 AS row_index
        FROM filtered
    ),
    limited AS (
        SELECT filtered.*
        FROM ordered
        JOIN filtered USING (event_id)
        WHERE ordered.row_index >= GREATEST(COALESCE(p_offset, 0), 0)
          AND ordered.row_index < GREATEST(COALESCE(p_offset, 0), 0)
              + LEAST(GREATEST(COALESCE(p_limit, 100), 1), 1000)
    ),
    remaining AS (
        SELECT filtered.*
        FROM ordered
        JOIN filtered USING (event_id)
        WHERE ordered.row_index >= GREATEST(COALESCE(p_offset, 0), 0)
              + LEAST(GREATEST(COALESCE(p_limit, 100), 1), 1000)
    ),
    cursor_values AS (
        SELECT r.stream_id,
               COALESCE(max(l.seq), 0) AS after_seq
        FROM remaining r
        LEFT JOIN limited l USING (stream_id)
        GROUP BY r.stream_id
    )
    SELECT jsonb_build_object(
        'events', COALESCE(
            (SELECT jsonb_agg(to_jsonb(e) ORDER BY e.stream_id, e.seq) FROM limited e),
            '[]'::jsonb
        ),
        'has_more', (SELECT count(*) FROM remaining) > 0,
        'next_cursor', (SELECT jsonb_object_agg(stream_id, after_seq) FROM cursor_values),
        'truncated', (SELECT count(*) FROM remaining) > 0,
        'filter_omitted_count',
            (SELECT count(*) FROM scope) - (SELECT count(*) FROM filtered)
    );
$$;

CREATE OR REPLACE FUNCTION replay_behavior_events(
    p_session_id UUID,
    p_stream_id TEXT DEFAULT NULL,
    p_limit INT DEFAULT 10000,
    p_cursor JSONB DEFAULT NULL
) RETURNS JSONB
LANGUAGE sql STABLE
AS $$
    WITH scope AS (
        SELECT e.*
        FROM behavior_events e
        WHERE (p_stream_id IS NULL OR e.stream_id = p_stream_id)
          AND (
                p_cursor IS NULL
                OR e.stream_id IN (SELECT key::text FROM jsonb_each_text(p_cursor))
              )
          AND (
                p_cursor IS NULL
                OR e.seq > (
                    SELECT (value)::bigint
                    FROM jsonb_each_text(p_cursor)
                    WHERE key = e.stream_id
                )
              )
    ),
    filtered AS (
        SELECT * FROM scope WHERE session_id = p_session_id
    ),
    numbered AS (
        SELECT stream_id, seq,
               LAG(seq) OVER (PARTITION BY stream_id ORDER BY seq) AS prev_seq
        FROM scope
    ),
    gaps AS (
        SELECT stream_id,
               jsonb_agg(
                   jsonb_build_object('after_seq', prev_seq, 'before_seq', seq)
                   ORDER BY seq
               ) AS sequence_gaps
        FROM numbered
        WHERE prev_seq IS NOT NULL AND seq > prev_seq + 1
        GROUP BY stream_id
    ),
    matched AS (
        SELECT filtered.*,
               row_number() OVER (ORDER BY stream_id, seq) - 1 AS row_index
        FROM filtered
    ),
    limited AS (
        SELECT * FROM matched
        WHERE row_index < LEAST(GREATEST(COALESCE(p_limit, 10000), 1), 10000)
    ),
    remaining AS (
        SELECT * FROM matched
        WHERE row_index >= LEAST(GREATEST(COALESCE(p_limit, 10000), 1), 10000)
    ),
    stream_events AS (
        SELECT stream_id, jsonb_agg(to_jsonb(e) ORDER BY e.seq) AS events
        FROM limited e
        GROUP BY stream_id
    ),
    cursor_values AS (
        SELECT r.stream_id,
               COALESCE(max(l.seq), 0) AS after_seq
        FROM remaining r
        LEFT JOIN limited l USING (stream_id)
        GROUP BY r.stream_id
    )
    SELECT jsonb_build_object(
        'schema_version', 1,
        'session_id', p_session_id,
        'stream_filter', p_stream_id,
        'event_count', (SELECT count(*) FROM limited),
        'events', COALESCE(
            (SELECT jsonb_agg(to_jsonb(e) ORDER BY e.stream_id, e.seq) FROM limited e),
            '[]'::jsonb
        ),
        'streams', COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'stream_id', se.stream_id,
                        'events', se.events,
                        'sequence_gaps', COALESCE(g.sequence_gaps, '[]'::jsonb)
                    ) ORDER BY se.stream_id
                )
                FROM stream_events se
                LEFT JOIN gaps g USING (stream_id)
            ),
            '[]'::jsonb
        ),
        'has_more', (SELECT count(*) FROM remaining) > 0,
        'next_cursor', (SELECT jsonb_object_agg(stream_id, after_seq) FROM cursor_values),
        'truncated', (SELECT count(*) FROM remaining) > 0,
        'filter_omitted_count',
            (SELECT count(*) FROM scope) - (SELECT count(*) FROM filtered)
    );
$$;
