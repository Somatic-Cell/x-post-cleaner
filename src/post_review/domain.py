from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import AppError

LABELS = {"DELETE_CANDIDATE", "KEEP", "NEEDS_CONTEXT"}


def digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def valid_id(value: Any) -> str:
    # IDs are strings, not IEEE-754 numbers. Reject bools and floats.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AppError("invalid_id")
    result = str(value)
    if not re.fullmatch(r"[0-9]{1,19}", result) or int(result) <= 0:
        raise AppError("invalid_id")
    return result


def canonical_text(text: str) -> str:
    # Do not strip, case-fold, normalize Unicode, or collapse whitespace: edits matter.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def display_text(text: str) -> str:
    """Escape terminal controls / bidi overrides without changing stored or classified text."""
    out: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        bidi = unicodedata.bidirectional(char)
        if char == "\n":
            out.append(char)
        elif char == "\t":
            out.append("    ")
        elif category == "Cc" or bidi in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}:
            out.append(f"[U+{ord(char):04X}]")
        else:
            out.append(char)
    return "".join(out)


def utc_string(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise AppError("naive_timestamp")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_timestamp(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # Parse English month names independently of the host locale (Windows included).
        try:
            parts = value.split()
            if len(parts) != 6:
                raise ValueError
            month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split().index(parts[1]) + 1
            iso = f"{parts[5]}-{month:02}-{int(parts[2]):02}T{parts[3]}{parts[4]}"
            dt = datetime.fromisoformat(iso)
        except (ValueError, TypeError, AttributeError):
            raise AppError("invalid_timestamp") from None
    if dt.tzinfo is None:
        raise AppError("naive_timestamp")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Period:
    since: datetime | None = None
    until: datetime | None = None
    timezone_name: str = "Asia/Tokyo"

    @classmethod
    def parse(cls, since: str | None, until: str | None, zone: str) -> Period:
        try:
            tz = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise AppError("unknown_timezone") from None

        def bound(value: str | None) -> datetime | None:
            if value is None:
                return None
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise AppError("invalid_period") from None
            if dt.tzinfo is None:
                first, second = dt.replace(tzinfo=tz, fold=0), dt.replace(tzinfo=tz, fold=1)
                # DST gap / overlap: require the caller to give an explicit offset.
                if first.utcoffset() != second.utcoffset():
                    raise AppError("ambiguous_local_time_use_offset")
                if first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != dt:
                    raise AppError("nonexistent_local_time_use_offset")
                dt = first
            return dt.astimezone(timezone.utc)

        start, end = bound(since), bound(until)
        if start is not None and end is not None and start >= end:
            raise AppError("empty_or_reversed_period")
        return cls(start, end, zone)

    def contains(self, value: str) -> bool:
        dt = parse_timestamp(value)
        return (self.since is None or dt >= self.since) and (self.until is None or dt < self.until)


@dataclass(frozen=True)
class Post:
    id: str
    owner_id: str
    created_at: str
    text: str
    flags: tuple[str, ...] = ()

    @property
    def revision(self) -> str:
        return digest(asdict(self))

    @property
    def is_repost(self) -> bool:
        return "repost_or_rt_prefix" in self.flags


@dataclass(frozen=True)
class Assessment:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float
    model: str
    input_hash: str
    translation: str | None = None
    usage: Mapping[str, int] | None = None
    origin: str = "jev"

    def __post_init__(self) -> None:
        # Copy before wrapping: a read-only view of the caller's dictionary
        # would still change when the caller mutates that dictionary.
        object.__setattr__(self, "probabilities", MappingProxyType(dict(self.probabilities)))
        if self.usage is not None:
            object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))

    def to_dict(self) -> dict[str, Any]:
        """Return independent, JSON-compatible data using the existing DB schema."""
        return {
            "choice": self.choice,
            "probabilities": dict(self.probabilities),
            "confidence": self.confidence,
            "model": self.model,
            "input_hash": self.input_hash,
            "translation": self.translation,
            "usage": dict(self.usage) if self.usage is not None else None,
            "origin": self.origin,
        }

    @classmethod
    def validate(cls, raw: dict[str, Any], **extra: Any) -> Assessment:
        try:
            choice, probs, confidence = raw["choice"], raw["probabilities"], raw["confidence"]
            if choice not in LABELS or not isinstance(probs, dict) or set(probs) != LABELS:
                raise ValueError
            values = [*probs.values(), confidence]
            if any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
                raise ValueError
            if not math.isclose(sum(probs.values()), 1.0, abs_tol=1e-5):
                raise ValueError
            if probs[choice] + 1e-7 < max(probs.values()):
                raise ValueError
            return cls(choice=choice, probabilities={k: float(v) for k, v in probs.items()},
                       confidence=float(confidence), **extra)
        except (KeyError, TypeError, ValueError):
            raise AppError("invalid_classification_response") from None

    @property
    def delete_probability(self) -> float:
        return self.probabilities["DELETE_CANDIDATE"]


@dataclass(frozen=True)
class Item:
    post: Post
    assessment: Assessment
    profile_id: str
    decision: str | None = None


def auto_eligible(item: Item, threshold: float) -> bool:
    # Model confidence is intentionally NOT used as P(DELETE_CANDIDATE).
    a = item.assessment
    return (a.origin == "jev" and a.choice == "DELETE_CANDIDATE"
            and a.delete_probability >= threshold and not item.post.flags
            and item.decision is None)
