from dataclasses import replace
from datetime import datetime, timezone

import pytest

from post_review.domain import Assessment, Period, auto_eligible, display_text, parse_timestamp, valid_id
from post_review.errors import AppError


def test_half_open_tokyo_period():
    p = Period.parse("2025-01-01", "2026-01-01", "Asia/Tokyo")
    assert p.contains("2024-12-31T15:00:00Z")
    assert not p.contains("2024-12-31T14:59:59Z")
    assert p.contains("2025-12-31T14:59:59Z")
    assert not p.contains("2025-12-31T15:00:00Z")


@pytest.mark.parametrize("start,end,zone", [
    ("2025-01-01", "2025-01-01", "Asia/Tokyo"),
    ("bad", None, "UTC"),
    (None, None, "Nowhere/Invalid"),
    ("2025-11-02T01:30:00", None, "America/New_York"),
    ("2025-03-09T02:30:00", None, "America/New_York"),
])
def test_invalid_period(start, end, zone):
    with pytest.raises(AppError):
        Period.parse(start, end, zone)


def test_explicit_offset_resolves_dst():
    p = Period.parse("2025-11-02T01:30:00-04:00", None, "America/New_York")
    assert p.since == datetime(2025, 11, 2, 5, 30, tzinfo=timezone.utc)


def test_legacy_timestamp_not_locale_dependent():
    assert parse_timestamp("Sat Jun 14 14:18:00 +0000 2025").hour == 14


@pytest.mark.parametrize("bad", [1.5, True, "0", "-3", "1 OR 1=1", "1" * 20, None])
def test_ids_are_not_floats_or_sql(bad):
    with pytest.raises(AppError):
        valid_id(bad)


def test_large_id_kept_exact():
    assert valid_id("1234567890123456789") == "1234567890123456789"


def test_display_neutralizes_terminal_and_bidi_controls():
    assert "\x1b" not in display_text("before\x1b[2Jafter")
    assert "[U+001B]" in display_text("\x1b[31m")
    assert "[U+202E]" in display_text("x\u202Ey")
    assert display_text("日本語\n本文") == "日本語\n本文"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, True, "0.9"])
def test_invalid_model_numbers(value):
    raw = {"choice": "KEEP", "probabilities": {"KEEP": value, "DELETE_CANDIDATE": 0.0,
                                                "NEEDS_CONTEXT": 0.0}, "confidence": 1.0}
    with pytest.raises(AppError):
        Assessment.validate(raw, model="m", input_hash="hash")


def test_probability_distribution_not_silently_renormalized():
    raw = {"choice": "KEEP", "probabilities": {"KEEP": 0.7, "DELETE_CANDIDATE": 0.1,
                                                "NEEDS_CONTEXT": 0.1}, "confidence": 0.5}
    with pytest.raises(AppError):
        Assessment.validate(raw, model="m", input_hash="hash")


def test_high_confidence_keep_is_not_delete(item):
    assessment = replace(item.assessment, choice="KEEP", confidence=1.0,
                         probabilities={"KEEP": 1.0, "DELETE_CANDIDATE": 0.0, "NEEDS_CONTEXT": 0.0})
    assert not auto_eligible(replace(item, assessment=assessment), 0.98)


@pytest.mark.parametrize("flag", ["reply_context_missing", "media_not_analyzed", "edited_post"])
def test_context_flags_block_auto(item, flag):
    assert not auto_eligible(replace(item, post=replace(item.post, flags=(flag,))), 0.98)


@pytest.mark.parametrize("decision", ["keep", "defer", "approve"])
def test_any_human_decision_blocks_auto(item, decision):
    assert not auto_eligible(replace(item, decision=decision), 0.98)


def test_threshold_uses_delete_probability_not_confidence(item):
    assert auto_eligible(item, 0.98)  # confidence is only 0.8, P(delete) is 0.99.
    assert not auto_eligible(item, 0.995)
