-- DB-authoritative behavior event ingestion, query, and replay entry points.
SET search_path = public, ag_catalog, "$user";

CREATE OR REPLACE FUNCTION record_behavior_event(
    p_event_id UUID,
    p_schema_version INT,
    p_event_type TEXT,
    p_stream_id TEXT,
    p_session_id UUID,
    p_request_id TEXT DEFAULT NULL,
    p_stimulus_revision INT DEFAULT NULL,
    p_input_turn_id TEXT DEFAULT NULL,
    p_response_id TEXT DEFAULT NULL,
    p_media_session_id TEXT DEFAULT NULL,
    p_output_epoch INT DEFAULT NULL,
    p_turn_id UUID DEFAULT NULL,
    p_task_id TEXT DEFAULT NULL,
    p_call_id TEXT DEFAULT NULL,
    p_ts TIMESTAMPTZ DEFAULT NULL,
    p_source TEXT DEFAULT NULL,
    p_producer_id TEXT DEFAULT NULL,
    p_producer_seq BIGINT DEFAULT NULL,
    p_channel TEXT DEFAULT NULL,
    p_payload JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_seq BIGINT;
    v_inserted_seq BIGINT;
    v_existing behavior_events%ROWTYPE;
    v_recorded_at TIMESTAMPTZ;
BEGIN
    p_event_type := btrim(COALESCE(p_event_type, ''));
    p_stream_id := btrim(COALESCE(p_stream_id, ''));
    p_source := btrim(COALESCE(p_source, ''));
    p_producer_id := btrim(COALESCE(p_producer_id, ''));
    p_channel := btrim(COALESCE(p_channel, ''));
    p_request_id := NULLIF(btrim(COALESCE(p_request_id, '')), '');
    p_input_turn_id := NULLIF(btrim(COALESCE(p_input_turn_id, '')), '');
    p_response_id := NULLIF(btrim(COALESCE(p_response_id, '')), '');
    p_media_session_id := NULLIF(btrim(COALESCE(p_media_session_id, '')), '');
    p_task_id := NULLIF(btrim(COALESCE(p_task_id, '')), '');
    p_call_id := NULLIF(btrim(COALESCE(p_call_id, '')), '');
    p_payload := COALESCE(p_payload, '{}'::jsonb);

    IF p_event_id IS NULL THEN
        RAISE EXCEPTION 'behavior event_id is required';
    END IF;
    IF p_session_id IS NULL THEN
        RAISE EXCEPTION 'behavior session_id is required';
    END IF;
    IF p_schema_version IS NULL OR p_schema_version <= 0 THEN
        RAISE EXCEPTION 'behavior schema_version must be positive';
    END IF;
    IF p_event_type = '' THEN
        RAISE EXCEPTION 'behavior event_type is required';
    END IF;
    IF p_stream_id = '' THEN
        RAISE EXCEPTION 'behavior stream_id is required';
    END IF;
    IF p_source = '' OR p_producer_id = '' OR p_channel = '' THEN
        RAISE EXCEPTION 'behavior source, producer_id, and channel are required';
    END IF;
    IF p_producer_seq IS NULL OR p_producer_seq < 0 THEN
        RAISE EXCEPTION 'behavior producer_seq must be non-negative';
    END IF;
    IF p_stimulus_revision IS NOT NULL AND p_stimulus_revision < 0 THEN
        RAISE EXCEPTION 'behavior stimulus_revision must be non-negative';
    END IF;
    IF p_output_epoch IS NOT NULL AND p_output_epoch < 0 THEN
        RAISE EXCEPTION 'behavior output_epoch must be non-negative';
    END IF;
    IF jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'behavior payload must be a JSON object';
    END IF;

    SELECT * INTO v_existing FROM behavior_events
    WHERE event_id = p_event_id FOR UPDATE;
    IF FOUND THEN
        IF v_existing.schema_version IS DISTINCT FROM p_schema_version
           OR v_existing.event_type IS DISTINCT FROM p_event_type
           OR v_existing.stream_id IS DISTINCT FROM p_stream_id
           OR v_existing.session_id IS DISTINCT FROM p_session_id
           OR v_existing.request_id IS DISTINCT FROM p_request_id
           OR v_existing.stimulus_revision IS DISTINCT FROM p_stimulus_revision
           OR v_existing.input_turn_id IS DISTINCT FROM p_input_turn_id
           OR v_existing.response_id IS DISTINCT FROM p_response_id
           OR v_existing.media_session_id IS DISTINCT FROM p_media_session_id
           OR v_existing.output_epoch IS DISTINCT FROM p_output_epoch
           OR v_existing.turn_id IS DISTINCT FROM p_turn_id
           OR v_existing.task_id IS DISTINCT FROM p_task_id
           OR v_existing.call_id IS DISTINCT FROM p_call_id
           OR v_existing.ts IS DISTINCT FROM p_ts
           OR v_existing.source IS DISTINCT FROM p_source
           OR v_existing.producer_id IS DISTINCT FROM p_producer_id
           OR v_existing.producer_seq IS DISTINCT FROM p_producer_seq
           OR v_existing.channel IS DISTINCT FROM p_channel
           OR v_existing.payload IS DISTINCT FROM p_payload THEN
            RAISE EXCEPTION 'behavior event id conflict: %', p_event_id;
        END IF;
        RETURN jsonb_build_object(
            'event_id', v_existing.event_id::text,
            'seq', v_existing.seq,
            'deduplicated', TRUE
        );
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(p_stream_id, 0));
    SELECT COALESCE(MAX(seq), 0) + 1 INTO v_seq
    FROM behavior_events WHERE stream_id = p_stream_id;

    v_inserted_seq := NULL;
    v_recorded_at := clock_timestamp();
    INSERT INTO behavior_events (
        event_id, schema_version, event_type, stream_id, session_id,
        request_id, stimulus_revision, input_turn_id, response_id,
        media_session_id, output_epoch, turn_id, task_id, call_id,
        seq, ts, recorded_at, source, producer_id, producer_seq, channel, payload
    ) VALUES (
        p_event_id, p_schema_version, p_event_type, p_stream_id, p_session_id,
        p_request_id, p_stimulus_revision, p_input_turn_id, p_response_id,
        p_media_session_id, p_output_epoch, p_turn_id, p_task_id, p_call_id,
        v_seq, p_ts, v_recorded_at, p_source, p_producer_id, p_producer_seq,
        p_channel, p_payload
    )
    ON CONFLICT (event_id) DO NOTHING
    RETURNING seq INTO v_inserted_seq;

    IF v_inserted_seq IS NULL THEN
        SELECT * INTO v_existing FROM behavior_events
        WHERE event_id = p_event_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'behavior event insert conflict without existing row: %', p_event_id;
        END IF;
        IF v_existing.schema_version IS DISTINCT FROM p_schema_version
           OR v_existing.event_type IS DISTINCT FROM p_event_type
           OR v_existing.stream_id IS DISTINCT FROM p_stream_id
           OR v_existing.session_id IS DISTINCT FROM p_session_id
           OR v_existing.request_id IS DISTINCT FROM p_request_id
           OR v_existing.stimulus_revision IS DISTINCT FROM p_stimulus_revision
           OR v_existing.input_turn_id IS DISTINCT FROM p_input_turn_id
           OR v_existing.response_id IS DISTINCT FROM p_response_id
           OR v_existing.media_session_id IS DISTINCT FROM p_media_session_id
           OR v_existing.output_epoch IS DISTINCT FROM p_output_epoch
           OR v_existing.turn_id IS DISTINCT FROM p_turn_id
           OR v_existing.task_id IS DISTINCT FROM p_task_id
           OR v_existing.call_id IS DISTINCT FROM p_call_id
           OR v_existing.ts IS DISTINCT FROM p_ts
           OR v_existing.source IS DISTINCT FROM p_source
           OR v_existing.producer_id IS DISTINCT FROM p_producer_id
           OR v_existing.producer_seq IS DISTINCT FROM p_producer_seq
           OR v_existing.channel IS DISTINCT FROM p_channel
           OR v_existing.payload IS DISTINCT FROM p_payload THEN
            RAISE EXCEPTION 'behavior event id conflict: %', p_event_id;
        END IF;
        RETURN jsonb_build_object(
            'event_id', v_existing.event_id::text,
            'seq', v_existing.seq,
            'deduplicated', TRUE
        );
    END IF;

    RETURN jsonb_build_object(
        'event_id', p_event_id::text,
        'seq', v_inserted_seq,
        'deduplicated', FALSE
    );
END;
$$;

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
