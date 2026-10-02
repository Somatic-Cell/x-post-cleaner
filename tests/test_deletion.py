from dataclasses import replace

import pytest

from post_review.deletion import DeletePolicy, DeletionService
from post_review.domain import Item
from post_review.errors import AppError, DeleteUnknown, ProviderError
from post_review.providers.x import LivePost


class FakeX:
    def __init__(self, post):
        self.owner = post.owner_id
        self.live = LivePost(post.id, post.owner_id, post.text, ())
        self.deletes = []
        self.error = None
        self.on_delete = lambda _: None

    async def me(self):
        return self.owner

    async def lookup(self, post_id):
        return self.live

    async def delete(self, post_id):
        self.on_delete(post_id)
        self.deletes.append(post_id)
        if self.error:
            raise self.error
        return None


def service(store, item, **kwargs):
    x = FakeX(item.post)
    policy = DeletePolicy(True, True, item.post.owner_id, auto_threshold=0.98, **kwargs)
    return DeletionService(store, x, policy), x


async def test_manual_requires_approval(store, item):
    s, x = service(store, item)
    with pytest.raises(AppError, match="requires_approval"):
        await s.delete(item)
    assert not x.deletes


async def test_manual_success_commits_before_and_after_request(store, item):
    s, x = service(store, item)
    store.decide(item, "approve")
    x.on_delete = lambda _: pytest_assert(store.deletion(item.post.id)["state"] == "in_flight")
    await s.delete(item)
    assert x.deletes == [item.post.id]
    assert store.deletion(item.post.id)["state"] == "deleted"
    assert float(store.get_meta("x_next_delete_at")) > 0


def pytest_assert(value):
    assert value


async def test_automatic_success(store, item):
    s, x = service(store, item)
    await s.delete(item, automatic=True)
    assert len(x.deletes) == 1
    assert store.decision(item.post) == "approve"


@pytest.mark.parametrize("decision", ["keep", "defer", "approve"])
async def test_auto_respects_human_decision(store, item, decision):
    s, x = service(store, item)
    store.decide(item, decision)
    with pytest.raises(AppError, match="not_eligible"):
        await s.delete(item, automatic=True)
    assert not x.deletes


async def test_authenticated_account_mismatch_never_deletes(store, item):
    s, x = service(store, item)
    x.owner = "999"
    store.decide(item, "approve")
    with pytest.raises(AppError, match="account_mismatch"):
        await s.delete(item)
    assert not x.deletes


@pytest.mark.parametrize("change,code", [
    ({"id": "999"}, "live_post_id_mismatch"),
    ({"author_id": "999"}, "live_post_owner_mismatch"),
    ({"text": "edited since archive"}, "live_text_mismatch"),
    ({"flags": ("repost_or_rt_prefix",)}, "repost_removal"),
    ({"flags": ("edited_post",)}, "edit_history"),
])
async def test_preflight_guards(store, item, change, code):
    s, x = service(store, item)
    store.decide(item, "approve")
    x.live = replace(x.live, **change)
    with pytest.raises(AppError, match=code):
        await s.delete(item)
    assert not x.deletes


async def test_auto_rechecks_current_context_flags(store, item):
    s, x = service(store, item)
    x.live = replace(x.live, flags=("media_not_analyzed",))
    with pytest.raises(AppError, match="live_context_flags"):
        await s.delete(item, automatic=True)
    assert not x.deletes


async def test_unknown_cannot_be_retried(store, item):
    s, x = service(store, item)
    store.decide(item, "approve")
    x.error = DeleteUnknown("simulated_timeout")
    with pytest.raises(DeleteUnknown):
        await s.delete(item)
    assert store.deletion(item.post.id)["state"] == "unknown"
    with pytest.raises(AppError, match="reconciliation"):
        await s.delete(item)
    assert len(x.deletes) == 1


async def test_known_failure_stays_failed_not_deleted(store, item):
    s, x = service(store, item)
    store.decide(item, "approve")
    x.error = ProviderError("x_delete_http_429", retry_at=9999999999.0)
    with pytest.raises(ProviderError):
        await s.delete(item)
    assert store.deletion(item.post.id)["state"] == "failed"
    assert store.get_meta("x_next_delete_at") == "9999999999.0"


async def test_unexpected_exception_after_request_intent_becomes_unknown(store, item):
    s, x = service(store, item)
    store.decide(item, "approve")
    x.error = RuntimeError("not printed")
    with pytest.raises(RuntimeError):
        await s.delete(item)
    assert store.deletion(item.post.id)["state"] == "unknown"


async def test_delete_cap_counts_requests(store, item):
    s, x = service(store, item, max_attempts=1)
    await s.delete(item, automatic=True)
    with pytest.raises(AppError, match="cap_reached"):
        await s.delete(item, automatic=True)
    assert len(x.deletes) == 1


async def test_changed_local_text_invalidates_delete(store, item):
    s, x = service(store, item)
    store.ingest(item.post.owner_id, [replace(item.post, text="new text")])
    with pytest.raises(AppError, match="stale_classification"):
        await s.delete(item, automatic=True)
    assert not x.deletes


def test_demo_cannot_enable_deletion(store, item):
    store.set_meta("demo", "true")
    with pytest.raises(AppError, match="demo_database"):
        service(store, item)


@pytest.mark.parametrize("enabled,ack", [(False, False), (True, False), (False, True)])
def test_both_opt_ins_required(enabled, ack):
    with pytest.raises(AppError):
        DeletePolicy(enabled, ack, "123").validate()


async def test_reconcile_does_not_delete_and_explicit_reset_required(store, item):
    s, x = service(store, item)
    store.decide(item, "approve")
    store.set_deletion(item, "unknown", "timeout")
    assert await s.reconcile(item.post.id, reset_if_present=False) == "present_and_matches"
    assert store.deletion(item.post.id)["state"] == "unknown"
    await s.reconcile(item.post.id, reset_if_present=True)
    assert store.deletion(item.post.id) is None
    assert store.decision(item.post) == "approve"  # prevents automatic deletion after reset
    assert not x.deletes


async def test_demo_profile_cannot_delete_even_without_demo_db_flag(store, item):
    profile = store.add_profile({"provider": "demo"})
    demo_item = Item(item.post, item.assessment, profile)
    s, x = service(store, item)
    with pytest.raises(AppError, match="non_jev_profile"):
        await s.delete(demo_item, automatic=True)
    assert not x.deletes
