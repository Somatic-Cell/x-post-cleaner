"""The only orchestration path that can issue a real DELETE request."""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Callable

from .domain import Item, Post, auto_eligible, valid_id
from .errors import AppError, DeleteUnknown, ProviderError
from .providers.x import LivePost, XClient
from .store import Store


@dataclass(frozen=True)
class DeletePolicy:
    enabled: bool
    acknowledge_irreversible: bool
    expected_owner_id: str
    max_attempts: int = 20
    minimum_interval: float = 18.1
    auto_threshold: float | None = None

    def validate(self) -> None:
        if not self.enabled or not self.acknowledge_irreversible:
            raise AppError("deletion_requires_enable_and_acknowledgement")
        valid_id(self.expected_owner_id)
        if self.max_attempts <= 0:
            raise AppError("positive_deletion_cap_required")
        if not math.isfinite(self.minimum_interval) or self.minimum_interval < 18.0:
            raise AppError("minimum_delete_interval_is_18_seconds")
        if self.auto_threshold is not None and (not math.isfinite(self.auto_threshold)
                                                 or not 0 <= self.auto_threshold <= 1):
            raise AppError("invalid_auto_threshold")


def verify_live(post: Post, live: LivePost) -> None:
    if live.id != post.id:
        raise AppError("live_post_id_mismatch")
    if live.author_id != post.owner_id:
        raise AppError("live_post_owner_mismatch")
    if live.text != post.text:
        raise AppError("live_text_mismatch_refresh_archive_and_reclassify")
    if "repost_or_rt_prefix" in live.flags or post.is_repost:
        raise AppError("repost_removal_not_implemented")
    if "edited_post" in live.flags:
        raise AppError("edit_history_requires_separate_review_not_supported")


class DeletionService:
    def __init__(self, store: Store, client: XClient, policy: DeletePolicy, *,
                 notice: Callable[[str], None] = lambda _: None):
        policy.validate()
        if store.get_meta("demo") == "true":
            raise AppError("demo_database_cannot_delete")
        if policy.expected_owner_id != store.owner_id:
            raise AppError("expected_owner_does_not_match_archive")
        self.store, self.client, self.policy, self.notice = store, client, policy, notice
        self.verified = False
        self.attempts = 0

    async def verify_account(self) -> None:
        if await self.client.me() != self.policy.expected_owner_id:
            raise AppError("authenticated_x_account_mismatch")
        self.verified = True

    async def _wait_turn(self) -> None:
        raw = self.store.get_meta("x_next_delete_at")
        deadline = float(raw) if raw else 0.0
        if not math.isfinite(deadline):
            raise AppError("invalid_persisted_rate_limit")
        while deadline > time.time():
            self.notice(f"API 待機中：あと約 {math.ceil(deadline - time.time())} 秒")
            await asyncio.sleep(min(1.0, deadline - time.time()))

    def _save_deadline(self, deadline: float | None) -> None:
        if deadline is not None:
            old = float(self.store.get_meta("x_next_delete_at") or 0.0)
            self.store.set_meta("x_next_delete_at", str(max(old, deadline)))

    async def delete(self, item: Item, *, automatic: bool = False) -> None:
        if self.attempts >= self.policy.max_attempts:
            raise AppError("session_deletion_cap_reached")
        if self.store.profile(item.profile_id).get("provider") != "jev":
            raise AppError("non_jev_profile_cannot_delete")
        current = self.store.post(item.post.id)
        if current.revision != item.post.revision:
            raise AppError("stale_classification")
        saved = self.store.assessment(current, item.profile_id)
        if saved != item.assessment:
            raise AppError("assessment_not_in_database")
        if self.store.deletion(current.id) is not None:
            raise AppError("previous_deletion_requires_reconciliation")
        decision = self.store.decision(current)
        if automatic:
            threshold = self.policy.auto_threshold
            if threshold is None:
                raise AppError("auto_requires_explicit_threshold")
            if decision is not None or not auto_eligible(item, threshold):
                raise AppError("not_eligible_for_automatic_deletion")
        elif decision != "approve":
            raise AppError("manual_deletion_requires_approval")
        if not self.verified:
            await self.verify_account()
        await self._wait_turn()
        self.notice("削除前に現在の本文と投稿者を照合しています")
        live = await self.client.lookup(current.id)
        verify_live(current, live)
        if automatic and live.flags:
            raise AppError("live_context_flags_require_manual_review")
        if automatic:
            self.store.decide(item, "approve")
        # Commit intent BEFORE the request. A crash after this point becomes UNKNOWN on restart.
        self.store.set_deletion(item, "in_flight", "request_intent_committed")
        self._save_deadline(time.time() + self.policy.minimum_interval)
        self.attempts += 1
        self.notice("削除要求中：処理が終わるまで次の操作を受け付けません")
        try:
            deadline = await self.client.delete(current.id)
        except DeleteUnknown as exc:
            self._save_deadline(exc.retry_at)
            self.store.set_deletion(item, "unknown", exc.code)
            raise
        except ProviderError as exc:
            self._save_deadline(exc.retry_at)
            self.store.set_deletion(item, "failed", exc.code)
            raise
        except BaseException:
            self.store.set_deletion(item, "unknown", "interrupted_or_unexpected_exception")
            raise
        self._save_deadline(deadline)
        self.store.set_deletion(item, "deleted", "api_confirmed_deleted_true")
        self.notice("削除成功")

    async def reconcile(self, post_id: str, *, reset_if_present: bool) -> str:
        state = self.store.deletion(post_id)
        if state is None:
            raise AppError("no_deletion_to_reconcile")
        if state["state"] == "deleted":
            raise AppError("confirmed_deletion_cannot_be_reset")
        if not self.verified:
            await self.verify_account()
        post = self.store.post(post_id)
        # 404/403 etc remain UNKNOWN/FAILED; they are not proof that our DELETE succeeded.
        live = await self.client.lookup(post.id)
        verify_live(post, live)
        if reset_if_present:
            self.store.reset_present_deletion(post.id)
        return "present_and_matches_reset" if reset_if_present else "present_and_matches"
