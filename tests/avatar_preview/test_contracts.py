import re
from pathlib import Path

import pytest

from services.avatar_emotion import (
    CONTEXT_KEYS,
    EMOTIONS,
    WORDSET_VERSION,
    select_context,
    validate_label,
)

EXPECTED_EMOTIONS = {
    "neutral",
    "happy",
    "excited",
    "curious",
    "surprised",
    "confused",
    "suspicious",
    "angry",
    "sad",
    "scared",
    "proud",
    "shy",
    "bored",
    "playful",
}


def test_contract_has_the_same_closed_v1_wordset():
    assert EMOTIONS == EXPECTED_EMOTIONS
    assert len(EMOTIONS) == 14
    assert WORDSET_VERSION == "v1"


def test_typescript_contract_wordset_matches_python_wordset():
    typescript = (
        Path(__file__).resolve().parents[2] / "hexis-ui/lib/avatar-preview/contracts.ts"
    )
    source = typescript.read_text(encoding="utf-8")
    block = re.search(r"export const EMOTIONS = \[(.*?)\] as const", source, re.DOTALL)
    assert block is not None
    frontend_emotions = set(re.findall(r'"([a-z]+)"', block.group(1)))
    assert frontend_emotions == EMOTIONS


def test_context_contract_contains_only_the_four_allowed_categories():
    assert CONTEXT_KEYS == ("identity", "worldview", "goals", "relationships")
    raw = {
        "identity": [],
        "worldview": {},
        "goals": [],
        "relationships": {},
        "emotional_state": {"angry": True},
        "urgent_drives": ["act"],
    }
    assert select_context(raw) == {key: raw[key] for key in CONTEXT_KEYS}


@pytest.mark.parametrize(
    "value", ["Happy", " happy", "happy\n", "joy", '{"label":"happy"}', None, 7]
)
def test_label_validation_rejects_aliases_and_non_string_values(value):
    with pytest.raises(ValueError):
        validate_label(value)


@pytest.mark.parametrize("value", sorted(EXPECTED_EMOTIONS))
def test_label_validation_accepts_each_exact_machine_value(value):
    assert validate_label(value) == value


@pytest.mark.parametrize(
    "raw",
    [
        {"identity": [], "worldview": [], "goals": []},
        '{"identity": []}',
        [],
        "not json",
    ],
)
def test_context_validation_rejects_missing_or_invalid_context(raw):
    with pytest.raises(ValueError):
        select_context(raw)
