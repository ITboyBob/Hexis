-- 0258_voice_realtime_sessions.sql
-- Qwen Realtime WS session lifecycle table for the M4 voice chain.
-- One active media generation per chat is enforced by a partial unique index.
CREATE TABLE IF NOT EXISTS voice_realtime_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    qwen_session_id TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'closed', 'failed')),
    closed_reason TEXT
        CHECK (closed_reason IS NULL OR closed_reason IN ('manual', 'disconnect', 'error')),
    rebuild_count INT NOT NULL DEFAULT 0,
    model TEXT,
    endpoint TEXT,
    instructions_hash TEXT,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMPTZ,
    last_event_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_vrs_session_active
    ON voice_realtime_sessions (session_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_vrs_session_created
    ON voice_realtime_sessions (session_id, created_at DESC);
