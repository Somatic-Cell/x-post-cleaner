from dataclasses import replace

import pytest

from post_review.domain import Period
from post_review.errors import AppError
from post_review.store import Store


def test_import_is_atomic(store, post):
    def broken():
        yield post
        raise AppError("parse_failure")
    with pytest.raises(AppError):
        store.ingest(post.owner_id, broken())
    assert store.counts()["posts"] == 0
    assert store.get_meta("owner_id") is None


def test_single_process_lock(tmp_path):
    path = tmp_path / "locked.sqlite3"
    with Store(path, create=True):
        with pytest.raises(AppError, match="database_in_use"):
            Store(path)


def test_owner_mismatch_rejected(store, post):
    store.ingest(post.owner_id, [post])
    with pytest.raises(AppError):
        store.ingest("999", [])


def test_unchanged_import_preserves_decision(store, item):
    store.decide(item, "keep")
    store.ingest(item.post.owner_id, [item.post])
    assert store.decision(item.post) == "keep"
    assert store.items(Period(), item.profile_id, 0) == []


def test_text_change_invalidates_old_classification_and_decision(store, item):
    store.decide(item, "keep")
    new = replace(item.post, text=item.post.text + "changed")
    store.ingest(new.owner_id, [new])
    assert store.assessment(new, item.profile_id) is None
    assert store.decision(new) is None


def test_keeping_post_applies_across_profiles(store, item, config):
    store.decide(item, "keep")
    new_config = dict(config, max_input_chars=12000)
    profile = store.add_profile(new_config)
    store.save_assessment(item.post, profile, item.assessment)
    assert store.items(Period(), profile, 0, all_posts=True) == []


def test_inflight_becomes_unknown_after_restart(tmp_path, post, config):
    from post_review.domain import Item
    from post_review.pipeline import guard_assessment
    path = tmp_path / "restart.sqlite3"
    with Store(path, create=True) as store:
        store.ingest(post.owner_id, [post])
        profile = store.add_profile(config)
        item = Item(post, guard_assessment(post, "test"), profile)
        store.set_deletion(item, "in_flight", "sent")
    with Store(path) as store:
        assert store.deletion(post.id)["state"] == "unknown"


def test_decisions_individually_committed(store, item):
    import sqlite3
    store.decide(item, "defer")
    external = sqlite3.connect(store.path)
    try:
        assert external.execute("SELECT value FROM decisions").fetchone()[0] == "defer"
    finally:
        external.close()


def test_unknown_and_failed_never_reappear_automatically(store, item):
    store.set_deletion(item, "unknown", "timeout")
    assert store.items(Period(), item.profile_id, 0, all_posts=True, include_approved=True) == []


def test_multiple_profiles_require_explicit_selection(store, config):
    profile = store.add_profile(config)
    assert store.resolve_profile(None) == profile
    store.add_profile(dict(config, max_input_chars=1))
    with pytest.raises(AppError, match="choose_explicit_profile"):
        store.resolve_profile(None)
    assert store.resolve_profile(profile[:16]) == profile


def test_deferred_and_approved_resume_only_when_requested(store, item):
    store.decide(item, "defer")
    assert not store.items(Period(), item.profile_id, 0)
    assert store.items(Period(), item.profile_id, 0, include_deferred=True)
    store.decide(item, "approve")
    assert not store.items(Period(), item.profile_id, 0)
    assert store.items(Period(), item.profile_id, 0, include_approved=True)
