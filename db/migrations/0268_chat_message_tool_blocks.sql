-- BOB-107 S1: durable content blocks and source-identified chat messages.
-- Forward-only migration. Existing message rows and receipts are not rewritten.
SET search_path = public, ag_catalog, "$user";

ALTER TABLE public.chat_messages
    ADD COLUMN IF NOT EXISTS content_blocks JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE public.chat_messages
    DROP CONSTRAINT IF EXISTS chat_messages_content_blocks_check;
ALTER TABLE public.chat_messages
    ADD CONSTRAINT chat_messages_content_blocks_check
    CHECK (jsonb_typeof(content_blocks) = 'array');

ALTER TABLE public.chat_messages
    DROP CONSTRAINT IF EXISTS chat_messages_role_check;
ALTER TABLE public.chat_messages
    ADD CONSTRAINT chat_messages_role_check
    CHECK (role IN ('system', 'user', 'assistant', 'toolResult'));

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.chat_messages
        WHERE source_message_id IS NOT NULL
        GROUP BY session_id, source_message_id
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'chat_messages contains duplicate non-null source identities; refusing to rewrite history';
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_session_source_message_id
    ON public.chat_messages (session_id, source_message_id)
    WHERE source_message_id IS NOT NULL;

CREATE OR REPLACE FUNCTION append_chat_message_blocks(
    p_session_id UUID,
    p_role TEXT,
    p_content TEXT,
    p_content_blocks JSONB,
    p_metadata JSONB DEFAULT '{}'::jsonb,
    p_source_message_id TEXT DEFAULT NULL,
    p_visible_in_context BOOLEAN DEFAULT TRUE
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    row_message chat_messages%ROWTYPE;
    next_ordinal INT;
    normalized_role TEXT := NULLIF(btrim(p_role), '');
    normalized_source TEXT := NULLIF(btrim(COALESCE(p_source_message_id, '')), '');
    normalized_blocks JSONB := p_content_blocks;
    normalized_metadata JSONB := COALESCE(p_metadata, '{}'::jsonb);
    block_value JSONB;
    text_projection TEXT;
    text_block_count INT;
    tool_call_count INT;
    distinct_call_count INT;
    turn_id UUID;
    tool_call_id TEXT;
    tool_name TEXT;
BEGIN
    IF p_session_id IS NULL THEN
        RAISE EXCEPTION 'session_id is required';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext(p_session_id::text));

    IF normalized_role IS NULL OR normalized_role NOT IN ('system', 'user', 'assistant', 'toolResult') THEN
        RAISE EXCEPTION 'chat message role must be system, user, assistant, or toolResult';
    END IF;
    IF p_content IS NULL THEN
        RAISE EXCEPTION 'chat message content is required';
    END IF;
    IF normalized_blocks IS NULL OR jsonb_typeof(normalized_blocks) <> 'array' THEN
        RAISE EXCEPTION 'content_blocks must be a JSON array';
    END IF;
    IF jsonb_typeof(normalized_metadata) <> 'object' THEN
        RAISE EXCEPTION 'message metadata must be a JSON object';
    END IF;

    FOR block_value IN SELECT value FROM jsonb_array_elements(normalized_blocks)
    LOOP
        IF jsonb_typeof(block_value) <> 'object' THEN
            RAISE EXCEPTION 'each content block must be an object';
        END IF;
        IF block_value->>'type' = 'text' THEN
            IF (SELECT count(*) FROM jsonb_object_keys(block_value)) <> 2
               OR jsonb_typeof(block_value->'text') <> 'string' THEN
                RAISE EXCEPTION 'text blocks require exactly type and string text';
            END IF;
        ELSIF block_value->>'type' = 'toolCall' THEN
            IF normalized_role <> 'assistant' THEN
                RAISE EXCEPTION 'toolCall blocks are allowed only on assistant messages';
            END IF;
            IF (SELECT count(*) FROM jsonb_object_keys(block_value)) <> 4
               OR jsonb_typeof(block_value->'id') <> 'string'
               OR NULLIF(btrim(block_value->>'id'), '') IS NULL
               OR jsonb_typeof(block_value->'name') <> 'string'
               OR NULLIF(btrim(block_value->>'name'), '') IS NULL
               OR jsonb_typeof(block_value->'arguments') <> 'object' THEN
                RAISE EXCEPTION 'toolCall requires id, name, and object arguments';
            END IF;
            tool_call_id := block_value->>'id';
        ELSE
            RAISE EXCEPTION 'unsupported content block type: %', COALESCE(block_value->>'type', '<missing>');
        END IF;
    END LOOP;

    SELECT count(*) FILTER (WHERE value->>'type'='text'),
           COALESCE(string_agg(value->>'text', '' ORDER BY ordinal), '')
    INTO text_block_count, text_projection
    FROM jsonb_array_elements(normalized_blocks) WITH ORDINALITY AS blocks(value, ordinal);
    IF text_block_count > 0 AND text_projection IS DISTINCT FROM p_content THEN
        RAISE EXCEPTION 'content must equal its text-block compatibility projection';
    END IF;

    SELECT count(*) FILTER (WHERE value->>'type'='toolCall'),
           count(DISTINCT value->>'id') FILTER (WHERE value->>'type'='toolCall')
    INTO tool_call_count, distinct_call_count
    FROM jsonb_array_elements(normalized_blocks) AS blocks(value);
    IF tool_call_count > distinct_call_count THEN
        RAISE EXCEPTION 'toolCall ids must be unique within one assistant message';
    END IF;
    IF tool_call_count > 1 THEN
        RAISE EXCEPTION 'persist each tool call as one source-identified assistant message';
    END IF;
    IF tool_call_count > 0 AND text_block_count = 0 AND p_content <> '' THEN
        RAISE EXCEPTION 'tool-only assistant content must be empty';
    END IF;

    IF normalized_metadata ? 'tool_result' THEN
        IF normalized_role <> 'toolResult' THEN
            RAISE EXCEPTION 'metadata.tool_result is allowed only on toolResult messages';
        END IF;
        IF jsonb_typeof(normalized_metadata->'tool_result') <> 'object'
           OR NOT (normalized_metadata->'tool_result' ? 'toolCallId')
           OR NOT (normalized_metadata->'tool_result' ? 'toolName')
           OR NOT (normalized_metadata->'tool_result' ? 'isError')
           OR NOT (normalized_metadata->'tool_result' ? 'details')
           OR jsonb_typeof(normalized_metadata#>'{tool_result,toolCallId}') <> 'string'
           OR NULLIF(btrim(normalized_metadata#>>'{tool_result,toolCallId}'), '') IS NULL
           OR jsonb_typeof(normalized_metadata#>'{tool_result,toolName}') <> 'string'
           OR NULLIF(btrim(normalized_metadata#>>'{tool_result,toolName}'), '') IS NULL
           OR jsonb_typeof(normalized_metadata#>'{tool_result,isError}') <> 'boolean'
           OR jsonb_typeof(normalized_metadata#>'{tool_result,details}') NOT IN ('object','array','null') THEN
            RAISE EXCEPTION 'tool_result must contain toolCallId, toolName, boolean isError, and object/array/null details';
        END IF;
        IF normalized_source IS NULL THEN
            RAISE EXCEPTION 'toolResult requires source_message_id';
        END IF;
        tool_call_id := normalized_metadata#>>'{tool_result,toolCallId}';
        tool_name := normalized_metadata#>>'{tool_result,toolName}';
    ELSIF normalized_role = 'toolResult' THEN
        RAISE EXCEPTION 'toolResult requires metadata.tool_result';
    END IF;

    IF normalized_metadata ? 'model_visual_attachments' THEN
        IF normalized_role <> 'toolResult'
           OR jsonb_typeof(normalized_metadata->'model_visual_attachments') <> 'array' THEN
            RAISE EXCEPTION 'model_visual_attachments must be a toolResult array';
        END IF;
        IF jsonb_array_length(normalized_metadata->'model_visual_attachments') > 4 THEN
            RAISE EXCEPTION 'model_visual_attachments cannot contain more than four items';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(normalized_metadata->'model_visual_attachments') AS attachments(value)
            WHERE jsonb_typeof(value) <> 'object'
               OR jsonb_typeof(value->'data_url') <> 'string'
               OR jsonb_typeof(value->'mime_type') <> 'string'
               OR COALESCE(value->>'data_url','') !~ '^data:image/(png|jpeg|webp|gif);base64,'
               OR COALESCE(value->>'mime_type','') !~ '^image/(png|jpeg|webp|gif)$'
               OR substring(value->>'data_url' from '^data:image/([^;]+);') <> split_part(value->>'mime_type','/',2)
               OR (value ? 'name' AND jsonb_typeof(value->'name') <> 'string')
        ) THEN
            RAISE EXCEPTION 'model_visual_attachments contains an unsupported image object';
        END IF;
    END IF;

    IF tool_call_count > 0 OR normalized_role = 'toolResult' THEN
        IF normalized_source IS NULL OR jsonb_typeof(normalized_metadata->'agent_turn_id') <> 'string' THEN
            RAISE EXCEPTION 'tool messages require source_message_id and actual agent_turn_id';
        END IF;
        BEGIN
            turn_id := (normalized_metadata->>'agent_turn_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'agent_turn_id must be a UUID';
        END;
        IF normalized_role = 'assistant'
           AND normalized_source <> format('tool:%s:%s:call', turn_id, tool_call_id) THEN
            RAISE EXCEPTION 'toolCall source_message_id does not match its turn and call id';
        END IF;
        IF normalized_role = 'toolResult'
           AND normalized_source <> format('tool:%s:%s:result', turn_id, tool_call_id) THEN
            RAISE EXCEPTION 'toolResult source_message_id does not match its turn and call id';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM agent_turns WHERE id=turn_id AND session_id=p_session_id) THEN
            RAISE EXCEPTION 'agent_turn_id does not belong to this chat session';
        END IF;
    END IF;

    PERFORM get_or_create_chat_session(p_session_id);
    IF normalized_source IS NOT NULL THEN
        SELECT * INTO row_message
        FROM chat_messages
        WHERE session_id = p_session_id AND source_message_id = normalized_source
        FOR UPDATE;
        IF FOUND THEN
            IF row_message.role = normalized_role
               AND row_message.content = p_content
               AND row_message.content_blocks = normalized_blocks
               AND row_message.metadata = normalized_metadata
               AND row_message.visible_in_context = COALESCE(p_visible_in_context, TRUE) THEN
                RETURN jsonb_build_object(
                    'message_id', row_message.id::text,
                    'session_id', row_message.session_id::text,
                    'ordinal', row_message.ordinal,
                    'role', row_message.role,
                    'content', row_message.content,
                    'content_blocks', row_message.content_blocks,
                    'source_message_id', row_message.source_message_id,
                    'visible_in_context', row_message.visible_in_context,
                    'metadata', row_message.metadata,
                    'created_at', row_message.created_at
                );
            END IF;
            RAISE EXCEPTION 'source_message_id conflict for session % and source %', p_session_id, normalized_source;
        END IF;
    END IF;

    IF tool_call_count > 0 THEN
        IF EXISTS (
            SELECT 1
            FROM chat_messages existing
            CROSS JOIN LATERAL jsonb_array_elements(existing.content_blocks) AS prior(block)
            WHERE existing.session_id = p_session_id
              AND existing.role = 'assistant'
              AND existing.metadata->>'agent_turn_id' = turn_id::text
              AND prior.block->>'type' = 'toolCall'
              AND prior.block->>'id' = tool_call_id
        ) THEN
            RAISE EXCEPTION 'tool call identity already exists for this session/turn/call id';
        END IF;
    END IF;

    IF normalized_role = 'toolResult' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM chat_messages call_message
            CROSS JOIN LATERAL jsonb_array_elements(call_message.content_blocks) AS prior(block)
            WHERE call_message.session_id = p_session_id
              AND call_message.role = 'assistant'
              AND call_message.metadata->>'agent_turn_id' = turn_id::text
              AND prior.block->>'type' = 'toolCall'
              AND prior.block->>'id' = tool_call_id
              AND prior.block->>'name' = tool_name
        ) THEN
            RAISE EXCEPTION 'toolResult has no matching call in this session and agent turn';
        END IF;
        IF EXISTS (
            SELECT 1 FROM chat_messages prior_result
            WHERE prior_result.session_id = p_session_id
              AND prior_result.role = 'toolResult'
              AND prior_result.metadata->>'agent_turn_id' = turn_id::text
              AND prior_result.metadata#>>'{tool_result,toolCallId}' = tool_call_id
        ) THEN
            RAISE EXCEPTION 'tool result identity already exists for this session/turn/call id';
        END IF;
    END IF;

    SELECT COALESCE(MAX(ordinal), -1) + 1 INTO next_ordinal
    FROM chat_messages WHERE session_id = p_session_id;
    INSERT INTO chat_messages (
        session_id, ordinal, role, content, content_blocks, metadata, source_message_id, visible_in_context
    ) VALUES (
        p_session_id, next_ordinal, normalized_role, p_content, normalized_blocks,
        normalized_metadata, normalized_source, COALESCE(p_visible_in_context, TRUE)
    ) RETURNING * INTO row_message;

    UPDATE chat_sessions
    SET updated_at = CURRENT_TIMESTAMP, last_active_at = CURRENT_TIMESTAMP
    WHERE id = p_session_id;

    RETURN jsonb_build_object(
        'message_id', row_message.id::text,
        'session_id', row_message.session_id::text,
        'ordinal', row_message.ordinal,
        'role', row_message.role,
        'content', row_message.content,
        'content_blocks', row_message.content_blocks,
        'source_message_id', row_message.source_message_id,
        'visible_in_context', row_message.visible_in_context,
        'metadata', row_message.metadata,
        'created_at', row_message.created_at
    );
END;
$$;

CREATE OR REPLACE FUNCTION append_chat_message(
    p_session_id UUID,
    p_role TEXT,
    p_content TEXT,
    p_metadata JSONB DEFAULT '{}'::jsonb,
    p_source_message_id TEXT DEFAULT NULL,
    p_visible_in_context BOOLEAN DEFAULT TRUE
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_role TEXT := lower(COALESCE(NULLIF(btrim(p_role), ''), ''));
BEGIN
    IF normalized_role NOT IN ('system', 'user', 'assistant') THEN
        RAISE EXCEPTION 'chat message role must be system, user, or assistant';
    END IF;
    RETURN append_chat_message_blocks(
        p_session_id, normalized_role, p_content, '[]'::jsonb,
        COALESCE(p_metadata, '{}'::jsonb), p_source_message_id, p_visible_in_context
    );
END;
$$;

CREATE OR REPLACE FUNCTION hydrate_chat_session(
    p_session_id UUID,
    p_limit INT DEFAULT NULL,
    p_include_system BOOLEAN DEFAULT FALSE
) RETURNS JSONB
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    row_session chat_sessions%ROWTYPE;
    lim INT := LEAST(
        GREATEST(COALESCE(p_limit, get_config_int('chat.session_history_limit'), 40), 1),
        200
    );
    messages JSONB;
BEGIN
    IF p_session_id IS NULL THEN
        RETURN jsonb_build_object('session_id', NULL, 'messages', '[]'::jsonb, 'count', 0);
    END IF;

    SELECT * INTO row_session
    FROM chat_sessions
    WHERE id = p_session_id
      AND status = 'active';

    IF NOT FOUND THEN
        RETURN jsonb_build_object('session_id', p_session_id::text, 'messages', '[]'::jsonb, 'count', 0);
    END IF;

    WITH selected AS (
        SELECT role, content, content_blocks, source_message_id, ordinal, id, created_at, metadata
        FROM chat_messages
        WHERE session_id = p_session_id
          AND visible_in_context
          AND (p_include_system OR role <> 'system')
        ORDER BY ordinal DESC
        LIMIT lim
    )
    SELECT COALESCE(jsonb_agg(
        jsonb_build_object(
            'role', role,
            'content', content,
            'content_blocks', content_blocks,
            'source_message_id', source_message_id,
            'ordinal', ordinal,
            'message_id', id::text,
            'created_at', created_at,
            'metadata', metadata
        )
        ORDER BY ordinal ASC
    ), '[]'::jsonb)
    INTO messages
    FROM selected;

    RETURN jsonb_build_object(
        'session_id', row_session.id::text,
        'surface', row_session.surface,
        'external_id', row_session.external_id,
        'messages', messages,
        'count', jsonb_array_length(messages),
        'cleared_at', row_session.cleared_at,
        'last_active_at', row_session.last_active_at
    );
END;
$$;

CREATE OR REPLACE FUNCTION get_chat_session_artifact(
    p_session_id UUID,
    p_include_messages BOOLEAN DEFAULT TRUE,
    p_include_hidden BOOLEAN DEFAULT TRUE
) RETURNS JSONB
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    row_session chat_sessions%ROWTYPE;
    messages JSONB := '[]'::jsonb;
    message_count INT := 0;
    visible_message_count INT := 0;
BEGIN
    IF p_session_id IS NULL THEN
        RETURN jsonb_build_object(
            'found', FALSE,
            'session_id', NULL,
            'reason', 'session_id_required'
        );
    END IF;

    SELECT *
    INTO row_session
    FROM chat_sessions
    WHERE id = p_session_id;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'found', FALSE,
            'session_id', p_session_id::text,
            'reason', 'not_found'
        );
    END IF;

    SELECT
        count(*)::int,
        count(*) FILTER (WHERE visible_in_context)::int
    INTO message_count, visible_message_count
    FROM chat_messages
    WHERE session_id = p_session_id;

    IF COALESCE(p_include_messages, TRUE) THEN
        SELECT COALESCE(jsonb_agg(
            jsonb_build_object(
                'message_id', id::text,
                'session_id', session_id::text,
                'ordinal', ordinal,
                'role', role,
                'content', content,
                'content_blocks', content_blocks,
                'visible_in_context', visible_in_context,
                'source_message_id', source_message_id,
                'metadata', metadata,
                'created_at', created_at,
                'updated_at', updated_at
            )
            ORDER BY ordinal ASC
        ), '[]'::jsonb)
        INTO messages
        FROM chat_messages
        WHERE session_id = p_session_id
          AND (COALESCE(p_include_hidden, TRUE) OR visible_in_context);
    END IF;

    RETURN jsonb_build_object(
        'found', TRUE,
        'format', 'hexis.chat_session.v1',
        'exported_at', CURRENT_TIMESTAMP,
        'message_count', message_count,
        'visible_message_count', visible_message_count,
        'include_hidden', COALESCE(p_include_hidden, TRUE),
        'session', jsonb_build_object(
            'session_id', row_session.id::text,
            'surface', row_session.surface,
            'external_id', row_session.external_id,
            'title', row_session.title,
            'status', row_session.status,
            'metadata', row_session.metadata,
            'created_at', row_session.created_at,
            'updated_at', row_session.updated_at,
            'last_active_at', row_session.last_active_at,
            'cleared_at', row_session.cleared_at
        ),
        'messages', messages
    );
END;
$$;

CREATE OR REPLACE FUNCTION fork_chat_session(
    p_source_session_id UUID,
    p_until_ordinal INT DEFAULT NULL,
    p_title TEXT DEFAULT NULL,
    p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    source_session chat_sessions%ROWTYPE;
    new_session chat_sessions%ROWTYPE;
    cut_ordinal INT := -1;
    copied_count INT := 0;
    artifact JSONB;
BEGIN
    IF p_source_session_id IS NULL THEN
        RETURN jsonb_build_object(
            'found', FALSE,
            'source_session_id', NULL,
            'reason', 'source_session_id_required'
        );
    END IF;

    SELECT *
    INTO source_session
    FROM chat_sessions
    WHERE id = p_source_session_id;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'found', FALSE,
            'source_session_id', p_source_session_id::text,
            'reason', 'not_found'
        );
    END IF;

    SELECT COALESCE(max(ordinal), -1)
    INTO cut_ordinal
    FROM chat_messages
    WHERE session_id = p_source_session_id;

    IF p_until_ordinal IS NOT NULL THEN
        cut_ordinal := LEAST(cut_ordinal, p_until_ordinal);
    END IF;

    INSERT INTO chat_sessions (
        surface,
        title,
        metadata
    )
    VALUES (
        source_session.surface,
        COALESCE(
            NULLIF(btrim(COALESCE(p_title, '')), ''),
            CASE
                WHEN source_session.title IS NOT NULL THEN source_session.title || ' (fork)'
                ELSE 'Fork of ' || left(source_session.id::text, 8)
            END
        ),
        source_session.metadata
            || jsonb_build_object(
                'forked_from_session_id', source_session.id::text,
                'forked_at', CURRENT_TIMESTAMP,
                'forked_until_ordinal', cut_ordinal
            )
            || COALESCE(p_metadata, '{}'::jsonb)
    )
    RETURNING * INTO new_session;

    INSERT INTO chat_messages (
        session_id,
        ordinal,
        role,
        content,
        content_blocks,
        visible_in_context,
        source_message_id,
        metadata
    )
    SELECT
        new_session.id,
        ordinal,
        role,
        content,
        content_blocks,
        visible_in_context,
        source_message_id,
        metadata || jsonb_build_object('forked_from_message_id', id::text)
    FROM chat_messages
    WHERE session_id = p_source_session_id
      AND ordinal <= cut_ordinal
    ORDER BY ordinal ASC;
    GET DIAGNOSTICS copied_count = ROW_COUNT;

    artifact := get_chat_session_artifact(new_session.id, TRUE, TRUE);
    RETURN artifact || jsonb_build_object(
        'source_session_id', source_session.id::text,
        'forked_message_count', copied_count
    );
END;
$$;
