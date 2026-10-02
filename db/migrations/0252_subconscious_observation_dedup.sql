-- 0252_subconscious_observation_dedup.sql
-- BOB-96: stop repeated strategic observations, make graph edges idempotent,
-- and route every contradiction through the durable review ledger.
-- This migration mirrors the changed baseline functions (they must win over
-- the earlier 0233 replay on fresh installs) and then performs the one-time
--存量 cleanup atomically in the same transaction.
SET search_path = public, ag_catalog, "$user";
SET check_function_bodies = off;

-- mirror: 05_functions_provenance_trust.sql::create_memory_relationship
CREATE OR REPLACE FUNCTION create_memory_relationship(
    p_from_id UUID,
    p_to_id UUID,
    p_relationship_type graph_edge_type,
    p_properties JSONB DEFAULT '{}'
) RETURNS VOID AS $$
DECLARE
    v_from UUID := p_from_id;
    v_to UUID := p_to_id;
    v_props JSONB := COALESCE(p_properties, '{}'::jsonb);
    v_set_clauses TEXT;
BEGIN
    -- CONTRADICTS is a semantic, unordered relation: normalize the endpoint pair
    -- so a reverse-order report updates the same edge identity instead of
    -- creating a second one. Other relationship types keep their direction.
    IF p_relationship_type = 'CONTRADICTS' THEN
        v_from := LEAST(p_from_id, p_to_id);
        v_to := GREATEST(p_from_id, p_to_id);
    END IF;

    -- The relational natural-key upsert runs first and takes the same-key row
    -- lock; the AGE write rides the same transaction so the two substrates
    -- cannot diverge. A CONTRADICTS report also clears any legacy reverse key.
    IF p_relationship_type = 'CONTRADICTS' THEN
        PERFORM delete_memory_edge('memory', p_to_id::text, 'CONTRADICTS', 'memory', p_from_id::text);
        PERFORM delete_memory_edge('memory', p_from_id::text, 'CONTRADICTS', 'memory', p_to_id::text);
    END IF;
    PERFORM upsert_memory_edge(v_from, v_to, p_relationship_type, v_props);

    -- Edge identity is (endpoints, type); properties are applied per key so a
    -- repeated call merges instead of duplicating. Empty properties emit no SET.
    SELECT string_agg(
               format('r.`%s` = %s', replace(key, '`', ''), value::text),
               ', '
           )
    INTO v_set_clauses
    FROM jsonb_each(v_props);

    EXECUTE format(
        'SELECT * FROM ag_catalog.cypher(''memory_graph'', $q$
            MATCH (a:MemoryNode {memory_id: %L}), (b:MemoryNode {memory_id: %L})
            MERGE (a)-[r:%s]->(b)
            %s
            RETURN r
        $q$) as (result ag_catalog.agtype)',
        v_from,
        v_to,
        p_relationship_type,
        CASE WHEN v_set_clauses IS NULL THEN '' ELSE 'SET ' || v_set_clauses END
    );
END;
$$ LANGUAGE plpgsql;


-- mirror: 05_functions_provenance_trust.sql::link_memory_to_concept
CREATE OR REPLACE FUNCTION link_memory_to_concept(
    p_memory_id UUID,
    p_concept_name TEXT,
    p_strength FLOAT DEFAULT 1.0
) RETURNS BOOLEAN AS $$
BEGIN
    -- Take the relational natural-key lock first so concurrent links to the
    -- same (memory, concept) serialize before the graph write. Failure rolls
    -- the whole function back and reports false.
    PERFORM upsert_memory_edge('memory', p_memory_id::text, 'INSTANCE_OF', 'concept', p_concept_name,
                               p_strength, NULL, NULL, jsonb_build_object('strength', p_strength));
    EXECUTE format(
        'SELECT * FROM ag_catalog.cypher(''memory_graph'', $q$
            MERGE (c:ConceptNode {name: %L})
            RETURN c
        $q$) as (result ag_catalog.agtype)',
        p_concept_name
    );
    EXECUTE format(
        'SELECT * FROM ag_catalog.cypher(''memory_graph'', $q$
            MATCH (m:MemoryNode {memory_id: %L})
            MATCH (c:ConceptNode {name: %L})
            MERGE (m)-[r:INSTANCE_OF]->(c)
            SET r.strength = %s
            RETURN m
        $q$) as (result ag_catalog.agtype)',
        p_memory_id,
        p_concept_name,
        p_strength
    );
    RETURN TRUE;
EXCEPTION
    WHEN OTHERS THEN
        RETURN FALSE;
END;
$$ LANGUAGE plpgsql;


-- mirror: 46j_functions_contradictions.sql::file_contradiction_case
CREATE OR REPLACE FUNCTION file_contradiction_case(
    p_memory_a UUID,
    p_memory_b UUID,
    p_new_memory_id UUID,
    p_tension TEXT,
    p_confidence FLOAT,
    p_detected_by TEXT DEFAULT 'model',
    p_metadata JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_case contradiction_cases%ROWTYPE;
    v_terminal contradiction_cases%ROWTYPE;
    v_pending contradiction_cases%ROWTYPE;
    v_confidence FLOAT := LEAST(1.0, GREATEST(0.0, COALESCE(p_confidence, 0.0)));
    v_threshold FLOAT := COALESCE(get_config_float('contradictions.minimum_confidence'), 0.78);
    v_pair_low UUID;
    v_pair_high UUID;
    v_created BOOLEAN := FALSE;
    v_new_memory UUID;
BEGIN
    IF p_memory_a IS NULL OR p_memory_b IS NULL OR p_memory_a = p_memory_b THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'invalid_pair');
    END IF;
    IF v_confidence < v_threshold THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'below_threshold', 'confidence', v_confidence);
    END IF;
    IF NULLIF(btrim(COALESCE(p_tension, '')), '') IS NULL THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'missing_tension');
    END IF;
    -- Episodic memories participate in the same user-adjudicated ledger; the
    -- detection queue/bootstrap stay semantic/worldview only.
    IF NOT EXISTS (
        SELECT 1 FROM memories
        WHERE id IN (p_memory_a, p_memory_b)
          AND type IN ('semantic', 'worldview', 'episodic')
          AND status = 'active'
          AND (valid_until IS NULL OR valid_until > CURRENT_TIMESTAMP)
        HAVING count(*) = 2
    ) THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'memory_not_found');
    END IF;

    -- One shared orchestration gate protects case creation and the terminal
    -- check for the whole transaction. It is re-entrant, so nested callers
    -- (the strategic AFTER INSERT trigger, imports) do not deadlock.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('hexis:memory_observation_orchestration', 0)
    );

    v_pair_low := LEAST(p_memory_a, p_memory_b);
    v_pair_high := GREATEST(p_memory_a, p_memory_b);
    v_new_memory := CASE WHEN p_new_memory_id IN (p_memory_a, p_memory_b) THEN p_new_memory_id ELSE NULL END;

    -- A terminal case (user decision or accepted tension) is permanent for
    -- this unordered pair: report it, never reopen, never add side effects.
    SELECT * INTO v_terminal
    FROM contradiction_cases
    WHERE LEAST(memory_a, memory_b) = v_pair_low
      AND GREATEST(memory_a, memory_b) = v_pair_high
      AND status IN ('resolved', 'tension')
    ORDER BY resolved_at DESC NULLS LAST, detected_at DESC, id DESC
    LIMIT 1;
    IF FOUND THEN
        RETURN jsonb_build_object(
            'created', FALSE,
            'reason', 'already_decided',
            'case_id', v_terminal.id,
            'code', contradiction_case_code(v_terminal.id),
            'status', v_terminal.status,
            'confidence', v_terminal.confidence
        );
    END IF;

    SELECT * INTO v_pending
    FROM contradiction_cases
    WHERE LEAST(memory_a, memory_b) = v_pair_low
      AND GREATEST(memory_a, memory_b) = v_pair_high
      AND status = 'pending'
    FOR UPDATE;

    IF FOUND THEN
        UPDATE contradiction_cases
        SET tension = btrim(p_tension),
            confidence = GREATEST(contradiction_cases.confidence, v_confidence),
            new_memory_id = COALESCE(v_new_memory, contradiction_cases.new_memory_id),
            metadata = contradiction_cases.metadata || COALESCE(p_metadata, '{}'::jsonb),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = v_pending.id
        RETURNING * INTO v_case;
    ELSE
        INSERT INTO contradiction_cases (
            memory_a, memory_b, new_memory_id, tension, confidence, detected_by, metadata
        ) VALUES (
            p_memory_a, p_memory_b, v_new_memory,
            btrim(p_tension), v_confidence,
            COALESCE(NULLIF(btrim(p_detected_by), ''), 'model'),
            COALESCE(p_metadata, '{}'::jsonb)
        )
        ON CONFLICT (LEAST(memory_a, memory_b), GREATEST(memory_a, memory_b))
            WHERE status = 'pending'
        DO NOTHING
        RETURNING * INTO v_case;
        v_created := FOUND;

        IF NOT v_created THEN
            -- Lost a concurrent race: the peer committed a pending case, so
            -- refresh it rather than attempting a second insert.
            SELECT * INTO v_pending
            FROM contradiction_cases
            WHERE LEAST(memory_a, memory_b) = v_pair_low
              AND GREATEST(memory_a, memory_b) = v_pair_high
              AND status = 'pending'
            FOR UPDATE;
            IF FOUND THEN
                UPDATE contradiction_cases
                SET tension = btrim(p_tension),
                    confidence = GREATEST(contradiction_cases.confidence, v_confidence),
                    new_memory_id = COALESCE(v_new_memory, contradiction_cases.new_memory_id),
                    metadata = contradiction_cases.metadata || COALESCE(p_metadata, '{}'::jsonb),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = v_pending.id
                RETURNING * INTO v_case;
            END IF;
        END IF;
    END IF;

    IF v_case.id IS NULL THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'existing_case');
    END IF;

    BEGIN
        PERFORM create_memory_relationship(
            v_case.memory_a,
            v_case.memory_b,
            'CONTRADICTS',
            jsonb_build_object(
                'confidence', v_case.confidence,
                'source', v_case.detected_by,
                'case_id', v_case.id,
                'tension', v_case.tension
            )
        );
    EXCEPTION WHEN OTHERS THEN
        -- The relational case is authoritative; AGE availability is advisory.
        RAISE WARNING 'contradiction edge sync failed for case %: %', v_case.id, SQLERRM;
    END;

    -- Only a genuinely new case raises coherence. Pending refreshes and trigger
    -- re-entry must not keep inflating the drive.
    IF v_created THEN
        UPDATE drives SET current_level = LEAST(1.0, current_level + 0.15)
        WHERE name = 'coherence';
    END IF;

    RETURN jsonb_strip_nulls(jsonb_build_object(
        'created', v_created,
        'reason', CASE WHEN v_created THEN NULL ELSE 'existing_case' END,
        'case_id', v_case.id,
        'code', contradiction_case_code(v_case.id),
        'status', v_case.status,
        'confidence', v_case.confidence
    ));
END;
$$;


-- mirror: 46j_functions_contradictions.sql::decide_contradiction
CREATE OR REPLACE FUNCTION decide_contradiction(
    p_case_id UUID,
    p_outcome TEXT,
    p_note TEXT DEFAULT NULL,
    p_decision_channel TEXT DEFAULT NULL,
    p_decision_actor TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_case contradiction_cases%ROWTYPE;
    v_outcome TEXT := lower(btrim(COALESCE(p_outcome, '')));
    v_new UUID;
    v_old UUID;
    v_winner UUID;
    v_loser UUID;
    v_resolution UUID;
    v_supersession UUID;
    v_actor TEXT := COALESCE(NULLIF(btrim(p_decision_actor), ''), 'user');
    v_note TEXT;
BEGIN
    IF v_outcome NOT IN ('new_right', 'old_right', 'tension') THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', 'invalid_outcome');
    END IF;
    -- Same orchestration gate as file/review/import entry points: a user
    -- decision can never interleave with a concurrent file of the same pair.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('hexis:memory_observation_orchestration', 0)
    );
    SELECT * INTO v_case
    FROM contradiction_cases
    WHERE id = p_case_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', 'case_not_found');
    END IF;
    IF v_case.status <> 'pending' THEN
        RETURN jsonb_build_object(
            'ok', TRUE, 'already_decided', TRUE, 'case_id', v_case.id,
            'status', v_case.status, 'outcome', v_case.outcome
        );
    END IF;

    v_new := COALESCE(v_case.new_memory_id, (
        SELECT id FROM memories
        WHERE id IN (v_case.memory_a, v_case.memory_b)
        ORDER BY created_at DESC, id DESC LIMIT 1
    ));
    v_old := CASE WHEN v_new = v_case.memory_a THEN v_case.memory_b ELSE v_case.memory_a END;
    IF v_outcome = 'new_right' THEN
        v_winner := v_new;
        v_loser := v_old;
    ELSIF v_outcome = 'old_right' THEN
        v_winner := v_old;
        v_loser := v_new;
    END IF;
    v_note := COALESCE(
        NULLIF(btrim(p_note), ''),
        CASE v_outcome
            WHEN 'new_right' THEN 'The newer memory is correct.'
            WHEN 'old_right' THEN 'The older memory remains correct.'
            ELSE 'Both memories remain valid in different contexts.'
        END
    );

    IF v_outcome <> 'tension' THEN
        v_supersession := record_supersession(
            v_loser,
            v_winner,
            'Contradiction decision ' || contradiction_case_code(v_case.id) || ': ' || v_note,
            v_actor,
            'active',
            CURRENT_TIMESTAMP,
            NULL,
            TRUE,
            jsonb_build_object('contradiction_case_id', v_case.id, 'outcome', v_outcome)
        );
    END IF;

    v_resolution := create_strategic_memory(
        p_content := v_note,
        p_pattern_description := CASE
            WHEN v_outcome = 'tension' THEN 'Contradiction accepted as contextual tension'
            ELSE 'Contradiction resolved by explicit user decision'
        END,
        p_confidence_score := 1.0,
        p_supporting_evidence := jsonb_build_object(
            'contradiction_case_id', v_case.id,
            'memory_a', v_case.memory_a,
            'memory_b', v_case.memory_b,
            'outcome', v_outcome,
            'winner_memory_id', v_winner,
            'loser_memory_id', v_loser
        ),
        p_importance := 0.7,
        p_source_attribution := jsonb_build_object(
            'kind', 'user_testimony',
            'ref', 'contradiction-decision:' || v_case.id::text,
            'label', 'Explicit contradiction decision',
            'trust', 1.0
        ),
        p_trust_level := 1.0
    );

    IF v_outcome <> 'tension' THEN
        BEGIN
            EXECUTE format(
                'SELECT * FROM ag_catalog.cypher(''memory_graph'', $q$
                    MATCH (a:MemoryNode {memory_id: %L})-[r:CONTRADICTS]-(b:MemoryNode {memory_id: %L})
                    DELETE r RETURN a
                $q$) as (result ag_catalog.agtype)',
                v_case.memory_a, v_case.memory_b
            );
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
        PERFORM delete_memory_edge('memory', v_case.memory_a::text, 'CONTRADICTS', 'memory', v_case.memory_b::text);
        PERFORM delete_memory_edge('memory', v_case.memory_b::text, 'CONTRADICTS', 'memory', v_case.memory_a::text);
    END IF;

    UPDATE contradiction_cases
    SET status = CASE WHEN v_outcome = 'tension' THEN 'tension' ELSE 'resolved' END,
        outcome = v_outcome,
        winner_memory_id = v_winner,
        loser_memory_id = v_loser,
        resolution_memory_id = v_resolution,
        supersession_id = v_supersession,
        resolution_note = v_note,
        decision_channel = NULLIF(btrim(COALESCE(p_decision_channel, '')), ''),
        decision_actor = v_actor,
        resolved_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = v_case.id;

    RETURN jsonb_strip_nulls(jsonb_build_object(
        'ok', TRUE,
        'case_id', v_case.id,
        'status', CASE WHEN v_outcome = 'tension' THEN 'tension' ELSE 'resolved' END,
        'outcome', v_outcome,
        'winner_memory_id', v_winner,
        'loser_memory_id', v_loser,
        'resolution_memory_id', v_resolution,
        'supersession_id', v_supersession
    ));
END;
$$;


-- mirror: 46j_functions_contradictions.sql::find_contradictions
CREATE OR REPLACE FUNCTION find_contradictions(p_memory_id UUID DEFAULT NULL)
RETURNS TABLE (
    memory_a UUID,
    memory_b UUID,
    content_a TEXT,
    content_b TEXT
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_filter TEXT;
BEGIN
    -- The review ledger is authoritative for every detected case.
    RETURN QUERY
    SELECT c.memory_a, c.memory_b, a.content, b.content
    FROM contradiction_cases c
    JOIN memories a ON a.id = c.memory_a
    JOIN memories b ON b.id = c.memory_b
    WHERE c.status = 'pending'
      AND (p_memory_id IS NULL OR p_memory_id IN (c.memory_a, c.memory_b))
    ORDER BY c.confidence DESC, c.detected_at, c.id;

    -- Preserve the long-standing public API for callers that explicitly create
    -- a CONTRADICTS graph edge through connect_memories(). Legacy graph-only
    -- pairs remain visible, but never duplicate a pending ledger case.
    v_filter := CASE
        WHEN p_memory_id IS NULL THEN ''
        ELSE format(
            'WHERE a.memory_id = %L OR b.memory_id = %L',
            p_memory_id,
            p_memory_id
        )
    END;
    BEGIN
        RETURN QUERY EXECUTE format($query$
            WITH graph_pairs AS (
                SELECT DISTINCT
                    LEAST(
                        replace(a_id::text, '"', '')::uuid,
                        replace(b_id::text, '"', '')::uuid
                    ) AS a_uuid,
                    GREATEST(
                        replace(a_id::text, '"', '')::uuid,
                        replace(b_id::text, '"', '')::uuid
                    ) AS b_uuid
                FROM ag_catalog.cypher('memory_graph', $cypher$
                    MATCH (a:MemoryNode)-[:CONTRADICTS]-(b:MemoryNode)
                    %s
                    RETURN a.memory_id, b.memory_id
                $cypher$) AS (a_id ag_catalog.agtype, b_id ag_catalog.agtype)
            )
            SELECT p.a_uuid, p.b_uuid, a.content, b.content
            FROM graph_pairs p
            JOIN memories a ON a.id = p.a_uuid
            JOIN memories b ON b.id = p.b_uuid
            WHERE NOT EXISTS (
                SELECT 1
                FROM contradiction_cases c
                WHERE LEAST(c.memory_a, c.memory_b) = p.a_uuid
                  AND GREATEST(c.memory_a, c.memory_b) = p.b_uuid
            )
        $query$, v_filter);
    EXCEPTION WHEN OTHERS THEN
        RAISE DEBUG 'Legacy contradiction graph lookup unavailable: %', SQLERRM;
    END;
END;
$$;


-- mirror: 46j_functions_contradictions.sql::execute_heartbeat_action
CREATE OR REPLACE FUNCTION execute_heartbeat_action(
    p_heartbeat_id UUID,
    p_action TEXT,
    p_params JSONB DEFAULT '{}'
)
RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_case_id UUID := _db_brain_try_uuid(p_params->>'case_id');
    v_memory_a UUID := _db_brain_try_uuid(p_params->>'memory_a');
    v_memory_b UUID := _db_brain_try_uuid(p_params->>'memory_b');
    v_filed JSONB;
    v_pair_low UUID;
    v_pair_high UUID;
    v_status TEXT;
BEGIN
    IF p_action NOT IN ('resolve_contradiction', 'accept_tension') THEN
        RETURN execute_heartbeat_action_legacy_contradictions(p_heartbeat_id, p_action, p_params);
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('hexis:memory_observation_orchestration', 0)
    );

    IF v_case_id IS NULL AND v_memory_a IS NOT NULL AND v_memory_b IS NOT NULL THEN
        v_pair_low := LEAST(v_memory_a, v_memory_b);
        v_pair_high := GREATEST(v_memory_a, v_memory_b);
        -- Terminal first: a decided pair is surfaced as already handled and is
        -- never re-proposed just because it is the newest ledger row.
        SELECT id, status INTO v_case_id, v_status
        FROM contradiction_cases
        WHERE LEAST(memory_a, memory_b) = v_pair_low
          AND GREATEST(memory_a, memory_b) = v_pair_high
        ORDER BY (status = 'pending') DESC,
                 resolved_at DESC NULLS LAST,
                 detected_at DESC,
                 id DESC
        LIMIT 1;
    END IF;
    IF v_case_id IS NULL AND v_memory_a IS NOT NULL AND v_memory_b IS NOT NULL THEN
        v_filed := file_contradiction_case(
            v_memory_a, v_memory_b, v_memory_b,
            COALESCE(NULLIF(p_params->>'resolution', ''), NULLIF(p_params->>'note', ''), 'Contradictory memories need a user decision.'),
            COALESCE(NULLIF(p_params->>'confidence', '')::float, 0.8),
            'heartbeat',
            jsonb_build_object('heartbeat_id', p_heartbeat_id)
        );
        v_case_id := _db_brain_try_uuid(v_filed->>'case_id');
        v_status := v_filed->>'status';
    END IF;
    IF v_case_id IS NULL THEN
        RETURN jsonb_build_object(
            'success', FALSE,
            'error', 'No pending contradiction case was found. Detection must file a case before review.',
            'action', p_action,
            'cost', 0,
            'energy_remaining', get_current_energy()
        );
    END IF;

    -- Re-read the true status for every branch (explicit id, pair lookup, or
    -- just-filed) so a terminal case is never marked decision_required.
    IF v_status IS NULL THEN
        SELECT status INTO v_status FROM contradiction_cases WHERE id = v_case_id;
    END IF;
    IF v_status IS NULL THEN
        RETURN jsonb_build_object(
            'success', FALSE,
            'error', 'No pending contradiction case was found. Detection must file a case before review.',
            'action', p_action,
            'cost', 0,
            'energy_remaining', get_current_energy()
        );
    END IF;

    IF v_status <> 'pending' THEN
        RETURN jsonb_build_object(
            'success', TRUE,
            'action', p_action,
            'cost', 0,
            'energy_remaining', get_current_energy(),
            'result', jsonb_build_object(
                'case_id', v_case_id,
                'decision_required', FALSE,
                'changed_memories', FALSE,
                'status', v_status,
                'next_step', 'This contradiction was already decided; the recorded decision stands.'
            ),
            'external_calls', '[]'::jsonb,
            'outbox_messages', '[]'::jsonb
        );
    END IF;

    UPDATE contradiction_cases
    SET proposed_at = COALESCE(proposed_at, CURRENT_TIMESTAMP),
        metadata = metadata || jsonb_build_object('noticed_by_heartbeat_id', p_heartbeat_id),
        updated_at = CURRENT_TIMESTAMP
    WHERE id = v_case_id AND status = 'pending';
    RETURN jsonb_build_object(
        'success', TRUE,
        'action', p_action,
        'cost', 0,
        'energy_remaining', get_current_energy(),
        'result', jsonb_build_object(
            'case_id', v_case_id,
            'decision_required', TRUE,
            'changed_memories', FALSE,
            'next_step', 'Include this case in the next daily contradiction review; only the user chooses new, old, or both.'
        ),
        'external_calls', '[]'::jsonb,
        'outbox_messages', '[]'::jsonb
    );
END;
$$;


-- mirror: 17_functions_subconscious_observations.sql::execute_heartbeat_actions_batch
CREATE OR REPLACE FUNCTION execute_heartbeat_actions_batch(
    p_heartbeat_id UUID,
    p_actions JSONB,
    p_start_index INT DEFAULT 0
)
RETURNS JSONB AS $$
DECLARE
    action_spec JSONB;
    action_name TEXT;
    action_params JSONB;
    action_result JSONB;
    actions_taken JSONB := '[]'::jsonb;
    outbox_messages JSONB := '[]'::jsonb;
    pending_external JSONB;
    action_external JSONB;
    next_index INT := COALESCE(p_start_index, 0);
    ord INT;
    halt_reason TEXT;
BEGIN
    IF p_actions IS NULL OR jsonb_typeof(p_actions) <> 'array' THEN
        RETURN jsonb_build_object(
            'actions_taken', '[]'::jsonb,
            'next_index', next_index,
            'outbox_messages', outbox_messages,
            'halt_reason', 'invalid_actions'
        );
    END IF;

    -- The built-in batch can file/decide contradictions, so it enters the same
    -- orchestration gate before touching any resource. Re-entrant with the
    -- per-action wrapper.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('hexis:memory_observation_orchestration', 0)
    );

    FOR action_spec, ord IN
        SELECT value, ordinality
        FROM jsonb_array_elements(p_actions) WITH ORDINALITY
        WHERE ordinality > COALESCE(p_start_index, 0)
        ORDER BY ordinality
    LOOP
        action_name := COALESCE(action_spec->>'action', 'rest');
        action_params := COALESCE(action_spec->'params', '{}'::jsonb);

        action_result := execute_heartbeat_action(p_heartbeat_id, action_name, action_params);
        actions_taken := actions_taken || jsonb_build_array(jsonb_build_object(
            'action', action_name,
            'params', action_params,
            'result', action_result
        ));
        next_index := ord;
        outbox_messages := outbox_messages || COALESCE(action_result->'outbox_messages', '[]'::jsonb);

        IF COALESCE((action_result->>'success')::boolean, FALSE) = FALSE THEN
            halt_reason := COALESCE(action_result->>'error', 'action_failed');
            RETURN jsonb_build_object(
                'actions_taken', actions_taken,
                'next_index', next_index,
                'outbox_messages', outbox_messages,
                'halt_reason', halt_reason
            );
        END IF;

        action_external := COALESCE(action_result->'external_calls', '[]'::jsonb);
        IF jsonb_typeof(action_external) = 'array' AND jsonb_array_length(action_external) > 0 THEN
            pending_external := action_external->0;
            RETURN jsonb_build_object(
                'actions_taken', actions_taken,
                'next_index', next_index,
                'pending_external_call', pending_external,
                'outbox_messages', outbox_messages,
                'halt_reason', 'external_call'
            );
        END IF;

        IF action_name = 'terminate'
            AND COALESCE((action_result#>>'{result,terminated}')::boolean, FALSE) THEN
            RETURN jsonb_build_object(
                'actions_taken', actions_taken,
                'next_index', next_index,
                'halt_reason', 'terminated'
            );
        END IF;

        IF action_name = 'pause_heartbeat'
            AND COALESCE((action_result#>>'{result,paused}')::boolean, FALSE) THEN
            RETURN jsonb_build_object(
                'actions_taken', actions_taken,
                'next_index', next_index,
                'halt_reason', 'paused'
            );
        END IF;
    END LOOP;

    RETURN jsonb_build_object(
        'actions_taken', actions_taken,
        'next_index', next_index,
        'outbox_messages', outbox_messages,
        'halt_reason', NULL
    );
END;
$$ LANGUAGE plpgsql;


-- mirror: 56a_functions_learning_reviews.sql::decide_learning_review_item
CREATE OR REPLACE FUNCTION decide_learning_review_item(
    p_item_id UUID,
    p_action TEXT,
    p_correction TEXT DEFAULT NULL,
    p_decision_channel TEXT DEFAULT NULL,
    p_decision_actor TEXT DEFAULT NULL,
    p_confirm_load_bearing BOOLEAN DEFAULT FALSE
)
RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_item learning_review_items%ROWTYPE;
    v_memory memories%ROWTYPE;
    v_action TEXT := lower(btrim(COALESCE(p_action, '')));
    v_correction TEXT := NULLIF(btrim(COALESCE(p_correction, '')), '');
    v_actor TEXT := COALESCE(NULLIF(btrim(COALESCE(p_decision_actor, '')), ''), 'user');
    v_new UUID;
    v_filed JSONB;
    v_decision JSONB;
    v_case UUID;
    v_supersession UUID;
    v_next_step TEXT;
BEGIN
    IF v_action NOT IN ('approve', 'correct', 'forget') THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', 'invalid_action');
    END IF;
    IF v_action = 'correct' AND v_correction IS NULL THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', 'correction_required');
    END IF;
    -- The weekly correction path can create/file contradictions after locking
    -- its own rows, so it takes the same orchestration gate before its first
    -- business lock to keep one global lock order.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('hexis:memory_observation_orchestration', 0)
    );
    SELECT * INTO v_item
    FROM learning_review_items
    WHERE id = p_item_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', 'item_not_found');
    END IF;
    IF v_item.status <> 'pending' THEN
        RETURN jsonb_build_object(
            'ok', TRUE,
            'already_decided', TRUE,
            'item_id', v_item.id,
            'status', v_item.status
        );
    END IF;

    IF v_item.kind = 'proposed_skill' THEN
        IF v_action = 'approve' THEN
            v_next_step := 'The skill was explicitly approved and is queued for application by the maintenance worker.';
        ELSE
            PERFORM transition_skill_improvement_proposal(
                v_item.source_skill_proposal_id,
                'reject'
            );
            v_next_step := CASE
                WHEN v_action = 'correct'
                    THEN 'The original skill proposal was rejected. The correction remains in the review record as evidence for a future revised proposal.'
                ELSE 'The skill proposal was rejected and retained in review history.'
            END;
        END IF;
    ELSE
        SELECT * INTO v_memory FROM memories WHERE id = v_item.source_memory_id FOR UPDATE;
        IF NOT FOUND THEN
            RETURN jsonb_build_object('ok', FALSE, 'error', 'source_memory_not_found');
        END IF;
        IF v_action = 'forget'
           AND is_memory_protected(v_memory.id)
           AND NOT COALESCE(p_confirm_load_bearing, FALSE) THEN
            RETURN jsonb_build_object(
                'ok', FALSE,
                'error', 'load_bearing_confirmation_required',
                'confirmation_required', TRUE,
                'item_id', v_item.id,
                'message', 'This memory is protected or load-bearing. Review its evidence and confirm forgetting explicitly; nothing changed.'
            );
        END IF;
        IF v_action = 'correct' THEN
            PERFORM record_memory_correction(
                v_memory.id,
                v_correction,
                'learning_review',
                jsonb_build_object(
                    'kind', 'user_testimony',
                    'ref', 'learning-review:' || v_item.id::text,
                    'label', 'Weekly learning review correction',
                    'trust', 1.0
                ),
                TRUE
            );
            v_new := create_memory(
                v_memory.type,
                v_correction,
                v_memory.importance,
                jsonb_build_object(
                    'kind', 'user_testimony',
                    'ref', 'learning-review:' || v_item.id::text,
                    'label', 'Explicit learning review correction',
                    'observed_at', CURRENT_TIMESTAMP,
                    'trust', 1.0
                ),
                1.0,
                jsonb_build_object(
                    'learning_review_correction', jsonb_build_object(
                        'item_id', v_item.id,
                        'corrects_memory_id', v_memory.id,
                        'actor', v_actor
                    ),
                    'confidence', 1.0
                )
            );
            IF v_memory.type IN ('semantic', 'worldview') THEN
                v_filed := file_contradiction_case(
                    v_memory.id,
                    v_new,
                    v_new,
                    'The explicit weekly review correction replaces the learned belief.',
                    1.0,
                    'user_learning_review',
                    jsonb_build_object('learning_review_item_id', v_item.id)
                );
                v_case := NULLIF(v_filed->>'case_id', '')::uuid;
                IF v_case IS NULL THEN
                    RAISE EXCEPTION 'could not file correction in contradiction ledger: %', v_filed;
                END IF;
                v_decision := decide_contradiction(
                    v_case,
                    'new_right',
                    'Explicit correction from weekly learning review: ' || v_correction,
                    p_decision_channel,
                    v_actor
                );
                IF NOT COALESCE((v_decision->>'ok')::boolean, FALSE) THEN
                    RAISE EXCEPTION 'could not resolve correction contradiction: %', v_decision;
                END IF;
            ELSE
                v_supersession := record_supersession(
                    v_memory.id,
                    v_new,
                    'Explicit correction from weekly learning review: ' || v_correction,
                    v_actor,
                    'active',
                    CURRENT_TIMESTAMP,
                    NULL,
                    TRUE,
                    jsonb_build_object('learning_review_item_id', v_item.id)
                );
                UPDATE memories SET status = 'archived' WHERE id = v_memory.id;
            END IF;
            v_next_step := 'The correction is active. The prior version remains queryable in memory history.';
        ELSIF v_action = 'forget' THEN
            UPDATE memories
            SET status = 'archived',
                valid_until = COALESCE(valid_until, CURRENT_TIMESTAMP),
                metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                    'forgotten_by_learning_review', jsonb_build_object(
                        'item_id', v_item.id,
                        'actor', v_actor,
                        'forgotten_at', CURRENT_TIMESTAMP
                    )
                ),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = v_memory.id;
            v_next_step := 'The learning left active recall but remains visible in point-in-time history.';
        ELSE
            v_next_step := 'The learning remains active and its evidence is unchanged.';
        END IF;
    END IF;

    UPDATE learning_review_items
    SET status = CASE v_action
            WHEN 'approve' THEN 'approved'
            WHEN 'correct' THEN 'corrected'
            ELSE 'forgotten'
        END,
        correction = CASE WHEN v_action = 'correct' THEN v_correction ELSE correction END,
        correction_memory_id = COALESCE(v_new, correction_memory_id),
        contradiction_case_id = COALESCE(v_case, contradiction_case_id),
        decision_channel = NULLIF(btrim(COALESCE(p_decision_channel, '')), ''),
        decision_actor = v_actor,
        decided_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP,
        metadata = metadata || jsonb_strip_nulls(jsonb_build_object(
            'supersession_id', v_supersession,
            'next_step', v_next_step
        ))
    WHERE id = v_item.id;

    UPDATE learning_reviews r
    SET status = 'completed', completed_at = CURRENT_TIMESTAMP
    WHERE r.id = v_item.review_id
      AND NOT EXISTS (
          SELECT 1 FROM learning_review_items i
          WHERE i.review_id = r.id AND i.status = 'pending'
      );

    RETURN jsonb_strip_nulls(jsonb_build_object(
        'ok', TRUE,
        'item_id', v_item.id,
        'review_id', v_item.review_id,
        'action', v_action,
        'status', CASE v_action
            WHEN 'approve' THEN 'approved'
            WHEN 'correct' THEN 'corrected'
            ELSE 'forgotten'
        END,
        'skill_proposal_id', v_item.source_skill_proposal_id,
        'correction_memory_id', v_new,
        'contradiction_case_id', v_case,
        'next_step', v_next_step
    ));
END;
$$;


-- mirror: 48_functions_memory_exchange.sql::hmx_import_memories
CREATE OR REPLACE FUNCTION hmx_import_memories(p_records JSONB) RETURNS JSONB AS $$
DECLARE
    item JSONB;
    local_id UUID;
    source_ref TEXT;
    metadata JSONB;
    ref_map JSONB := '{}'::jsonb;
    duplicate_refs JSONB := '[]'::jsonb;
    errors JSONB := '[]'::jsonb;
    inserted_count INT := 0;
BEGIN
    -- The strategic AFTER INSERT trigger can file a contradiction mid-import;
    -- take the shared orchestration gate before any record is processed so
    -- this import cannot invert lock order against a concurrent decision.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('hexis:memory_observation_orchestration', 0)
    );
    FOR item IN SELECT value FROM jsonb_array_elements(COALESCE(p_records, '[]'::jsonb))
    LOOP
        BEGIN
            source_ref := item->>'ref';
            IF NULLIF(source_ref, '') IS NULL OR NULLIF(item->>'content', '') IS NULL THEN
                RAISE EXCEPTION 'memory ref and content are required';
            END IF;

            SELECT m.id INTO local_id
            FROM memories m
            WHERE regexp_replace(lower(btrim(m.content)), '\s+', ' ', 'g') =
                  item->>'_transient_normalized_content'
            ORDER BY m.created_at, m.id
            LIMIT 1;

            IF local_id IS NOT NULL THEN
                ref_map := ref_map || jsonb_build_object(source_ref, local_id::text);
                duplicate_refs := duplicate_refs || jsonb_build_array(source_ref);
                CONTINUE;
            END IF;

            metadata := COALESCE(item->'metadata', '{}'::jsonb)
                || jsonb_build_object(
                    'embedding_status', 'pending_import',
                    'hmx', COALESCE(item->'metadata'->'hmx', '{}'::jsonb)
                        || jsonb_build_object('content_hash_v1', item->>'content_hash_v1')
                );
            metadata := jsonb_set(
                metadata,
                '{provenance}',
                COALESCE(item->'provenance', '{}'::jsonb),
                true
            );

            INSERT INTO memories (
                type, status, content, embedding, importance, trust_level,
                decay_rate, created_at, updated_at, valid_from, valid_until,
                access_count, last_accessed, source_attribution, metadata
            ) VALUES (
                (item->>'type')::memory_type,
                CASE WHEN item->>'status' IN ('active','archived','invalidated','staged')
                     THEN (item->>'status')::memory_status ELSE 'active'::memory_status END,
                item->>'content',
                array_fill(0.0::float, ARRAY[embedding_dimension()])::vector,
                COALESCE((item->>'importance')::float, 0.5),
                LEAST(1.0, GREATEST(0.0, COALESCE((item->>'trust_level')::float, 0.5))),
                COALESCE((item->>'decay_rate')::float, 0.01),
                COALESCE((item->>'created_at')::timestamptz, CURRENT_TIMESTAMP),
                COALESCE((item->>'updated_at')::timestamptz,
                         (item->>'created_at')::timestamptz, CURRENT_TIMESTAMP),
                NULLIF(item->>'valid_from', '')::timestamptz,
                NULLIF(item->>'valid_until', '')::timestamptz,
                GREATEST(COALESCE((item->>'access_count')::int, 0), 0),
                NULLIF(item->>'last_accessed', '')::timestamptz,
                COALESCE(item->'source_attribution', '{}'::jsonb),
                metadata
            ) RETURNING id INTO local_id;

            PERFORM sync_memory_node(local_id);
            INSERT INTO memory_neighborhoods (memory_id, is_stale)
            VALUES (local_id, TRUE)
            ON CONFLICT (memory_id) DO UPDATE SET is_stale = TRUE;

            ref_map := ref_map || jsonb_build_object(source_ref, local_id::text);
            inserted_count := inserted_count + 1;
        EXCEPTION WHEN OTHERS THEN
            errors := errors || jsonb_build_array(jsonb_build_object(
                'ref', source_ref, 'error', SQLERRM
            ));
        END;
    END LOOP;

    RETURN jsonb_build_object(
        'ref_map', ref_map,
        'inserted', inserted_count,
        'duplicate_refs', duplicate_refs,
        'errors', errors
    );
END;
$$ LANGUAGE plpgsql;


