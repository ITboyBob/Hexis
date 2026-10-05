-- Global behavior event log. Producer identity is authoritative; the DB adds
-- only reception order and persistence time.
SET search_path = public, ag_catalog, "$user";

CREATE TABLE IF NOT EXISTS behavior_events (
    event_id UUID PRIMARY KEY,
    schema_version INT NOT NULL CHECK (schema_version > 0),
    event_type TEXT NOT NULL
        CHECK (btrim(event_type) <> '' AND event_type = btrim(event_type)),
    stream_id TEXT NOT NULL
        CHECK (btrim(stream_id) <> '' AND stream_id = btrim(stream_id)),
    session_id UUID NOT NULL,
    request_id TEXT,
    stimulus_revision INT CHECK (stimulus_revision IS NULL OR stimulus_revision >= 0),
    input_turn_id TEXT,
    response_id TEXT,
    media_session_id TEXT,
    output_epoch INT CHECK (output_epoch IS NULL OR output_epoch >= 0),
    turn_id UUID REFERENCES agent_turns(id) ON DELETE SET NULL,
    task_id TEXT,
    call_id TEXT,
    seq BIGINT NOT NULL CHECK (seq > 0),
    ts TIMESTAMPTZ,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    source TEXT NOT NULL
        CHECK (btrim(source) <> '' AND source = btrim(source)),
    producer_id TEXT NOT NULL
        CHECK (btrim(producer_id) <> '' AND producer_id = btrim(producer_id)),
    producer_seq BIGINT NOT NULL CHECK (producer_seq >= 0),
    channel TEXT NOT NULL
        CHECK (btrim(channel) <> '' AND channel = btrim(channel)),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object'),
    UNIQUE (stream_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_behavior_events_session_recorded
    ON behavior_events (session_id, recorded_at DESC, event_id);
CREATE INDEX IF NOT EXISTS idx_behavior_events_type_recorded
    ON behavior_events (event_type, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_behavior_events_turn
    ON behavior_events (turn_id, seq) WHERE turn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_behavior_events_response
    ON behavior_events (response_id, recorded_at) WHERE response_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_behavior_events_task
    ON behavior_events (task_id, recorded_at) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_behavior_events_call
    ON behavior_events (call_id, recorded_at) WHERE call_id IS NOT NULL;
