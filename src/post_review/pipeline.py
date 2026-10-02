from __future__ import annotations

from typing import Callable

from .domain import Assessment, Period, Post, digest
from .errors import AppError
from .providers.azure import AzureTranslator
from .providers.jev import JevClassifier
from .store import Store


def guard_assessment(post: Post, reason: str) -> Assessment:
    # A routing decision, NOT a prediction made by Jev. UI must label it accordingly.
    return Assessment("NEEDS_CONTEXT", {"DELETE_CANDIDATE": 0.0, "KEEP": 0.0, "NEEDS_CONTEXT": 1.0},
                      0.0, "local-guard:" + reason, digest({"revision": post.revision, "reason": reason}),
                      origin="local_guard")


async def analyze(store: Store, period: Period, classifier: JevClassifier,
                  translator: AzureTranslator | None = None, *, limit: int | None = None,
                  progress: Callable[[dict[str, int]], None] = lambda _: None) -> tuple[str, dict[str, int]]:
    config = classifier.config
    profile = store.add_profile(config)
    counts = {"new": 0, "cached": 0, "reposts_skipped": 0, "local_guard": 0, "errors": 0}
    for post in store.posts(period):
        if post.is_repost:
            counts["reposts_skipped"] += 1
            continue
        if store.assessment(post, profile) is not None:
            counts["cached"] += 1
            continue
        if limit is not None and counts["new"] >= limit:
            break
        translation = None
        try:
            if not post.text or len(post.text) > config["max_input_chars"]:
                answer = guard_assessment(post, "empty_or_input_character_limit")
            else:
                if config["input_mode"] != "original":
                    if translator is None:
                        raise AppError("translation_required_no_fallback")
                    key = translator.cache_key(post.text)
                    translation = store.translation(key)
                    if translation is None:
                        translation = await translator.translate(post.text)
                        store.save_translation(key, translation)
                total = (len(post.text) if config["input_mode"] != "english" else 0) + len(translation or "")
                if total > config["max_input_chars"]:
                    answer = guard_assessment(post, "combined_input_character_limit")
                else:
                    answer = await classifier.classify(post, translation)
            store.save_assessment(post, profile, answer)
            counts["new"] += 1
            if answer.origin == "local_guard":
                counts["local_guard"] += 1
            progress(counts)
        except AppError as exc:
            store.save_assessment(post, profile, None, exc.code)
            counts["errors"] += 1
            progress(counts)
            # Stop on the first API/config error; retrying the command reuses completed rows.
            raise
    return profile, counts
