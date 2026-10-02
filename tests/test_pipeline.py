from dataclasses import replace

import pytest

from post_review.domain import Assessment, Period, digest
from post_review.errors import ProviderError
from post_review.pipeline import analyze


class FakeClassifier:
    def __init__(self, config):
        self.config, self.calls, self.error = config, [], None

    async def classify(self, post, translation=None):
        self.calls.append((post.id, translation))
        if self.error:
            raise self.error
        return Assessment("KEEP", {"KEEP": 0.9, "DELETE_CANDIDATE": 0.05, "NEEDS_CONTEXT": 0.05},
                          0.7, self.config["model"], digest((post.text, translation)), translation=translation)


class FakeTranslator:
    def __init__(self):
        self.calls = []

    def cache_key(self, text):
        return digest(("synthetic-translation", text))

    async def translate(self, text):
        self.calls.append(text)
        return "English translation"


async def test_classification_cache_survives_repeat(store, post, config):
    store.ingest(post.owner_id, [post])
    classifier = FakeClassifier(config)
    profile, counts = await analyze(store, Period(), classifier)
    assert counts["new"] == 1
    profile2, counts = await analyze(store, Period(), classifier)
    assert profile == profile2 and counts["cached"] == 1
    assert len(classifier.calls) == 1


async def test_rule_change_creates_new_classification_profile(store, post, config):
    store.ingest(post.owner_id, [post])
    first, _ = await analyze(store, Period(), FakeClassifier(config))
    changed = dict(config, rules=dict(config["rules"], version="changed"))
    second, counts = await analyze(store, Period(), FakeClassifier(changed))
    assert first != second and counts["new"] == 1


async def test_translation_cache_shared_between_english_and_both(store, post, config):
    store.ingest(post.owner_id, [post])
    translator = FakeTranslator()
    a, _ = await analyze(store, Period(), FakeClassifier(dict(config, input_mode="english")), translator)
    b, _ = await analyze(store, Period(), FakeClassifier(dict(config, input_mode="both")), translator)
    assert a != b and len(translator.calls) == 1


async def test_limit_counts_only_new_assessments(store, post, config):
    posts = [replace(post, id=str(100 + i)) for i in range(3)]
    store.ingest(post.owner_id, posts)
    classifier = FakeClassifier(config)
    _, counts = await analyze(store, Period(), classifier, limit=1)
    assert counts["new"] == 1
    _, counts = await analyze(store, Period(), classifier, limit=1)
    assert counts["cached"] == 1 and counts["new"] == 1
    assert len(classifier.calls) == 2


async def test_oversized_input_is_not_silently_truncated_or_sent(store, post, config):
    post = replace(post, text="長文" * 100)
    store.ingest(post.owner_id, [post])
    classifier = FakeClassifier(dict(config, max_input_chars=10))
    profile, counts = await analyze(store, Period(), classifier)
    a = store.assessment(post, profile)
    assert not classifier.calls
    assert counts["local_guard"] == 1
    assert a.origin == "local_guard" and a.choice == "NEEDS_CONTEXT"
    assert store.post(post.id).text == post.text


async def test_api_error_stays_unclassified_and_stops(store, post, config):
    store.ingest(post.owner_id, [post, replace(post, id="987654321")])
    classifier = FakeClassifier(config)
    classifier.error = ProviderError("jev_http_401")
    with pytest.raises(ProviderError):
        await analyze(store, Period(), classifier)
    assert len(classifier.calls) == 1
    row = store.conn.execute("SELECT payload,error_code FROM assessments").fetchone()
    assert row["payload"] is None and row["error_code"] == "jev_http_401"


async def test_reposts_not_uploaded(store, post, config):
    post = replace(post, flags=("repost_or_rt_prefix",))
    store.ingest(post.owner_id, [post])
    classifier = FakeClassifier(config)
    _, counts = await analyze(store, Period(), classifier)
    assert not classifier.calls and counts["reposts_skipped"] == 1


async def test_combined_translation_length_guard(store, post, config):
    post = replace(post, text="短文")
    store.ingest(post.owner_id, [post])
    classifier = FakeClassifier(dict(config, input_mode="both", max_input_chars=10))
    profile, _ = await analyze(store, Period(), classifier, FakeTranslator())
    assert not classifier.calls
    assert store.assessment(post, profile).origin == "local_guard"
