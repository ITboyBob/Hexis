-- 将矛盾检测默认间隔改为八小时，并支持三类记忆与 episodic 历史回填。
-- 保留实例显式配置、已有批次启动时间以及所有既有队列项的状态和结果。

INSERT INTO config_defaults (key, value, description) VALUES
    ('contradictions.detection_interval_seconds', '28800'::jsonb,
     '模型矛盾检测批次的最小启动间隔，默认八小时。')
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    description = EXCLUDED.description;

UPDATE config_defaults
SET description = '将新增的 episodic、semantic、worldview 记忆加入矛盾检测队列。'
WHERE key = 'contradictions.enabled';

CREATE OR REPLACE FUNCTION enqueue_contradiction_detection(p_memory_id UUID)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_memory memories%ROWTYPE;
    v_id UUID;
BEGIN
    IF NOT COALESCE(get_config_bool('contradictions.enabled'), TRUE) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO v_memory
    FROM memories
    WHERE id = p_memory_id
      AND type IN ('episodic', 'semantic', 'worldview')
      AND status = 'active'
      AND (valid_until IS NULL OR valid_until > CURRENT_TIMESTAMP);
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    INSERT INTO contradiction_detection_queue (memory_id, content_hash)
    VALUES (v_memory.id, encode(digest(v_memory.content, 'sha256'), 'hex'))
    ON CONFLICT (memory_id, content_hash) DO UPDATE
    SET status = CASE
            WHEN contradiction_detection_queue.status = 'failed'
              OR (contradiction_detection_queue.status = 'completed'
                  AND v_memory.embedding_status = 'embedded')
            THEN 'pending'
            ELSE contradiction_detection_queue.status
        END,
        next_attempt_at = CASE
            WHEN contradiction_detection_queue.status = 'failed'
              OR (contradiction_detection_queue.status = 'completed'
                  AND v_memory.embedding_status = 'embedded')
            THEN CURRENT_TIMESTAMP
            ELSE contradiction_detection_queue.next_attempt_at
        END,
        completed_at = CASE
            WHEN contradiction_detection_queue.status IN ('failed', 'completed')
             AND v_memory.embedding_status = 'embedded'
            THEN NULL
            ELSE contradiction_detection_queue.completed_at
        END,
        updated_at = CURRENT_TIMESTAMP
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$$;

CREATE OR REPLACE FUNCTION queue_memory_contradiction_detection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.type IN ('episodic', 'semantic', 'worldview')
       AND NEW.status = 'active'
       AND (TG_OP = 'INSERT'
            OR OLD.content IS DISTINCT FROM NEW.content
            OR OLD.embedding_status IS DISTINCT FROM NEW.embedding_status
            OR OLD.status IS DISTINCT FROM NEW.status
            OR OLD.type IS DISTINCT FROM NEW.type) THEN
        PERFORM enqueue_contradiction_detection(NEW.id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_queue_memory_contradiction_detection ON memories;
CREATE TRIGGER trg_queue_memory_contradiction_detection
AFTER INSERT OR UPDATE OF content, embedding_status, status, type ON memories
FOR EACH ROW EXECUTE FUNCTION queue_memory_contradiction_detection();

CREATE OR REPLACE FUNCTION contradiction_candidate_memories(
    p_memory_id UUID,
    p_limit INT DEFAULT NULL
)
RETURNS TABLE (
    memory_id UUID,
    content TEXT,
    memory_type memory_type,
    trust_level FLOAT,
    source_attribution JSONB,
    similarity FLOAT,
    lexical_rank FLOAT
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_limit INT := LEAST(GREATEST(COALESCE(
        p_limit, get_config_int('contradictions.candidates_per_memory'), 8
    ), 1), 30);
    v_threshold FLOAT := COALESCE(get_config_float('contradictions.candidate_similarity'), 0.55);
BEGIN
    RETURN QUERY
    WITH subject AS (
        SELECT m.id, m.content, m.embedding, m.embedding_status,
               websearch_to_tsquery('english', m.content) AS query
        FROM memories m
        WHERE m.id = p_memory_id
    ), scored AS (
        SELECT m.id, m.content, m.type, m.trust_level, m.source_attribution,
               CASE
                   WHEN s.embedding_status = 'embedded'
                    AND m.embedding_status = 'embedded'
                    AND s.embedding IS NOT NULL AND m.embedding IS NOT NULL
                   THEN (1.0 - (m.embedding <=> s.embedding))::float
                   ELSE NULL::float
               END AS semantic_similarity,
               ts_rank_cd(to_tsvector('english', m.content), s.query, 32)::float AS word_rank
        FROM subject s
        JOIN memories m ON m.id <> s.id
        WHERE m.type IN ('episodic', 'semantic', 'worldview')
          AND m.status = 'active'
          AND (m.valid_until IS NULL OR m.valid_until > CURRENT_TIMESTAMP)
          AND NOT EXISTS (
              SELECT 1 FROM contradiction_cases c
              WHERE LEAST(c.memory_a, c.memory_b) = LEAST(s.id, m.id)
                AND GREATEST(c.memory_a, c.memory_b) = GREATEST(s.id, m.id)
                AND c.status IN ('pending', 'tension')
          )
    )
    SELECT s.id, s.content, s.type, s.trust_level, s.source_attribution,
           s.semantic_similarity, s.word_rank
    FROM scored s
    WHERE COALESCE(s.semantic_similarity >= v_threshold, FALSE)
       OR s.word_rank > 0
    ORDER BY GREATEST(COALESCE(s.semantic_similarity, 0), s.word_rank) DESC,
             s.trust_level DESC,
             s.id
    LIMIT v_limit;
END;
$$;

CREATE OR REPLACE FUNCTION claim_contradiction_detection_batch(
    p_limit INT DEFAULT NULL,
    p_force BOOLEAN DEFAULT FALSE
)
RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_limit INT := LEAST(GREATEST(COALESCE(
        p_limit, get_config_int('contradictions.detection_batch_size'), 20
    ), 1), 100);
    v_interval INT := LEAST(GREATEST(COALESCE(
        get_config_int('contradictions.detection_interval_seconds'), 28800
    ), 10), 604800);
    v_state JSONB;
    v_last TIMESTAMPTZ;
    v_result JSONB;
BEGIN
    IF NOT COALESCE(get_config_bool('contradictions.enabled'), TRUE) THEN
        RETURN jsonb_build_object('skipped', TRUE, 'reason', 'disabled', 'items', '[]'::jsonb);
    END IF;
    INSERT INTO state (key, value)
    VALUES ('contradiction_detection_state', '{}'::jsonb)
    ON CONFLICT (key) DO NOTHING;
    SELECT value INTO v_state
    FROM state
    WHERE key = 'contradiction_detection_state'
    FOR UPDATE;
    v_last := NULLIF(v_state->>'last_batch_started_at', '')::timestamptz;
    IF NOT COALESCE(p_force, FALSE)
       AND v_last IS NOT NULL
       AND CURRENT_TIMESTAMP < v_last + make_interval(secs => v_interval) THEN
        RETURN jsonb_build_object('skipped', TRUE, 'reason', 'not_due', 'items', '[]'::jsonb);
    END IF;

    WITH picked AS (
        SELECT q.id
        FROM contradiction_detection_queue q
        JOIN memories m ON m.id = q.memory_id
        WHERE (
                (q.status = 'pending' AND q.next_attempt_at <= CURRENT_TIMESTAMP)
                OR (
                    q.status = 'processing'
                    AND q.claimed_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes'
                )
              )
          AND m.type IN ('episodic', 'semantic', 'worldview')
          AND m.status = 'active'
          AND (m.valid_until IS NULL OR m.valid_until > CURRENT_TIMESTAMP)
        ORDER BY q.created_at, q.id
        FOR UPDATE OF q SKIP LOCKED
        LIMIT v_limit
    ), claimed AS (
        UPDATE contradiction_detection_queue q
        SET status = 'processing',
            attempts = attempts + 1,
            claimed_at = CURRENT_TIMESTAMP,
            error = NULL,
            updated_at = CURRENT_TIMESTAMP
        FROM picked p
        WHERE q.id = p.id
        RETURNING q.id, q.memory_id, q.attempts
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'queue_id', c.id,
        'memory', jsonb_build_object(
            'memory_id', m.id,
            'content', m.content,
            'type', m.type,
            'created_at', m.created_at,
            'trust_level', m.trust_level,
            'source_attribution', m.source_attribution
        ),
        'candidates', COALESCE((
            SELECT jsonb_agg(to_jsonb(candidate))
            FROM contradiction_candidate_memories(m.id) candidate
        ), '[]'::jsonb),
        'attempt', c.attempts
    ) ORDER BY m.created_at, c.id), '[]'::jsonb)
    INTO v_result
    FROM claimed c
    JOIN memories m ON m.id = c.memory_id;

    PERFORM set_state(
        'contradiction_detection_state',
        v_state || jsonb_build_object(
            'last_batch_started_at', CURRENT_TIMESTAMP,
            'last_batch_size', jsonb_array_length(v_result)
        )
    );
    RETURN jsonb_build_object(
        'skipped', jsonb_array_length(v_result) = 0,
        'reason', CASE WHEN jsonb_array_length(v_result) = 0 THEN 'empty_queue' ELSE NULL END,
        'minimum_confidence', COALESCE(get_config_float('contradictions.minimum_confidence'), 0.78),
        'items', v_result
    );
END;
$$;

-- 一次性回填全部符合条件的既有 episodic；不重置或重跑已存在的队列项。
INSERT INTO contradiction_detection_queue (memory_id, content_hash)
SELECT m.id, encode(digest(m.content, 'sha256'), 'hex')
FROM memories m
WHERE m.type = 'episodic'
  AND m.status = 'active'
  AND (m.valid_until IS NULL OR m.valid_until > CURRENT_TIMESTAMP)
ORDER BY m.created_at, m.id
ON CONFLICT (memory_id, content_hash) DO NOTHING;
