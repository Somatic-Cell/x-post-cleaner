"""Read-only assessments must remain compatible with replacement and SQLite."""
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from post_review.domain import Assessment


def make_assessment(probabilities=None, usage=None):
    if probabilities is None:
        probabilities = {"DELETE_CANDIDATE": 0.99, "KEEP": 0.005, "NEEDS_CONTEXT": 0.005}
    return Assessment("DELETE_CANDIDATE", probabilities, 0.8, "m", "hash", usage=usage)


def test_constructor_does_not_retain_caller_dictionary():
    probabilities = {"DELETE_CANDIDATE": 0.99, "KEEP": 0.005, "NEEDS_CONTEXT": 0.005}
    assessment = make_assessment(probabilities)
    probabilities["DELETE_CANDIDATE"] = 0.0
    assert assessment.delete_probability == 0.99


def test_probabilities_cannot_be_modified():
    assessment = make_assessment()
    with pytest.raises(TypeError):
        assessment.probabilities["DELETE_CANDIDATE"] = 0.0


def test_frozen_fields_cannot_be_reassigned():
    assessment = make_assessment()
    with pytest.raises(FrozenInstanceError):
        assessment.choice = "KEEP"


def test_usage_is_copied_and_read_only():
    usage = {"input_tokens": 120}
    assessment = make_assessment(usage=usage)
    usage["input_tokens"] = 0
    assert assessment.usage["input_tokens"] == 120
    with pytest.raises(TypeError):
        assessment.usage["input_tokens"] = 0


def test_validated_assessment_is_read_only():
    raw = {"choice": "KEEP", "probabilities": {"KEEP": 1.0, "DELETE_CANDIDATE": 0.0,
                                               "NEEDS_CONTEXT": 0.0}, "confidence": 0.8}
    assessment = Assessment.validate(raw, model="m", input_hash="hash")
    raw["probabilities"]["KEEP"] = 0.0
    assert assessment.probabilities["KEEP"] == 1.0
    with pytest.raises(TypeError):
        assessment.probabilities["DELETE_CANDIDATE"] = 1.0


def test_exported_dictionaries_are_independent():
    assessment = make_assessment(usage={"input_tokens": 120})
    exported = assessment.to_dict()
    exported["probabilities"]["DELETE_CANDIDATE"] = 0.0
    exported["usage"]["input_tokens"] = 0
    assert assessment.delete_probability == 0.99
    assert assessment.usage["input_tokens"] == 120


def test_dataclasses_replace_still_works():
    original = make_assessment(usage={"input_tokens": 120})
    changed = replace(original, confidence=0.7)
    assert original.confidence == 0.8
    assert changed.confidence == 0.7
    assert changed.probabilities == original.probabilities
    assert changed.usage == original.usage
    with pytest.raises(TypeError):
        changed.probabilities["DELETE_CANDIDATE"] = 0.0


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 120}])
def test_sqlite_round_trip_preserves_format_and_immutability(store, post, config, usage):
    store.ingest(post.owner_id, [post])
    profile = store.add_profile(config)
    original = make_assessment(usage=usage)
    store.save_assessment(post, profile, original)
    payload = json.loads(store.conn.execute("SELECT payload FROM assessments").fetchone()[0])
    assert payload == {
        "choice": "DELETE_CANDIDATE",
        "probabilities": {"DELETE_CANDIDATE": 0.99, "KEEP": 0.005, "NEEDS_CONTEXT": 0.005},
        "confidence": 0.8,
        "model": "m",
        "input_hash": "hash",
        "translation": None,
        "usage": usage,
        "origin": "jev",
    }
    restored = store.assessment(post, profile)
    assert restored == original
    with pytest.raises(TypeError):
        restored.probabilities["DELETE_CANDIDATE"] = 0.0
    if restored.usage is not None:
        with pytest.raises(TypeError):
            restored.usage["input_tokens"] = 0
