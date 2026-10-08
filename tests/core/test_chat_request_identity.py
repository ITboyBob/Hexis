from __future__ import annotations

import pytest

from core.conversation_messages import (
    ConversationMessageError,
    resolve_chat_request_identity,
)


SESSION_ID = "10000000-0000-4000-8000-000000000001"
CLIENT_ID = "30000000-0000-4000-8000-000000000001"
TURN_ID = "20000000-0000-4000-8000-000000000001"


def test_explicit_client_message_id_is_stable_across_retries():
    first = resolve_chat_request_identity(SESSION_ID, CLIENT_ID)
    retry = resolve_chat_request_identity(SESSION_ID, CLIENT_ID)

    assert first == retry
    assert first["client_message_id"] == CLIENT_ID
    assert first["source_identity"] == f"chat:{SESSION_ID}:{CLIENT_ID}"
    assert first["user_source_message_id"] == f"chat:{SESSION_ID}:{CLIENT_ID}:user"
    assert first["assistant_source_message_id"] == f"chat:{SESSION_ID}:{CLIENT_ID}:assistant"
    assert first["legacy_client_message_id"] is False


def test_same_text_with_different_client_ids_has_distinct_sources():
    first = resolve_chat_request_identity(SESSION_ID, CLIENT_ID)
    second_id = "30000000-0000-4000-8000-000000000002"
    second = resolve_chat_request_identity(SESSION_ID, second_id)

    assert first["source_identity"] != second["source_identity"]


def test_legacy_request_gets_per_execution_identity_without_claiming_retry_dedupe():
    first = resolve_chat_request_identity(SESSION_ID, None)
    second = resolve_chat_request_identity(SESSION_ID, None)

    assert first["legacy_client_message_id"] is True
    assert second["legacy_client_message_id"] is True
    assert first["client_message_id"] != second["client_message_id"]
    assert first["source_identity"] != second["source_identity"]


@pytest.mark.parametrize("value", ["", "not-a-uuid", "30000000-0000-4000-8000-00000000000z"])
def test_invalid_explicit_client_message_id_is_rejected(value):
    with pytest.raises(ConversationMessageError) as error:
        resolve_chat_request_identity(SESSION_ID, value)
    assert error.value.category == "invalid_client_message_id"


def test_invalid_session_identity_is_rejected():
    with pytest.raises(ConversationMessageError) as error:
        resolve_chat_request_identity("chat:bad-session", CLIENT_ID)
    assert error.value.category == "invalid_session_identity"


def test_public_chat_request_accepts_client_id_but_not_turn_identity():
    from pydantic import ValidationError

    from apps.hexis_api import ChatRequest

    request = ChatRequest.model_validate({
        "message": "hello",
        "session_id": SESSION_ID,
        "client_message_id": CLIENT_ID,
    })
    assert str(request.client_message_id) == CLIENT_ID

    with pytest.raises(ValidationError):
        ChatRequest.model_validate({
            "message": "hello",
            "session_id": SESSION_ID,
            "agent_turn_id": TURN_ID,
        })

    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": "hello", "client_message_id": "not-a-uuid"})
