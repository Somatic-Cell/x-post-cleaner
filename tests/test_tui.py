"""Headless UI tests. Requires installation with [tui]; CI installs it explicitly."""
import pytest

pytest.importorskip("textual", reason="Textual optional dependency is not installed")

from textual.widgets import Input, Static
from post_review.domain import Period
from post_review.demo import seed_demo
from post_review.tui import ReviewApp


async def test_enter_keeps_and_clears_input(store):
    profile = seed_demo(store)
    items = store.items(Period(), profile, 0, all_posts=True)
    app = ReviewApp(store, items)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert store.decision(items[0].post) == "keep"
        assert app.position == 1
        assert app.query_one("#answer", Input).value == ""


async def test_y_records_approval_without_deleting(store):
    profile = seed_demo(store)
    items = store.items(Period(), profile, 0, all_posts=True)
    app = ReviewApp(store, items)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.press("y", "enter")
        await pilot.pause()
        assert store.decision(items[0].post) == "approve"
        assert store.deletion(items[0].post.id) is None
        assert app.position == 1


async def test_s_defers_and_moves_to_next(store):
    profile = seed_demo(store)
    items = store.items(Period(), profile, 0, all_posts=True)
    app = ReviewApp(store, items)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.press("s", "enter")
        await pilot.pause()
        assert store.decision(items[0].post) == "defer" and app.position == 1


async def test_invalid_key_does_not_decide(store):
    profile = seed_demo(store)
    items = store.items(Period(), profile, 0, all_posts=True)
    app = ReviewApp(store, items)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.press("z", "enter")
        await pilot.pause()
        assert store.decision(items[0].post) is None and app.position == 0


async def test_empty_queue_and_small_terminal(store):
    seed_demo(store)
    app = ReviewApp(store, [])
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert app.position == 0


async def test_long_post_and_controls_are_not_truncated_in_storage(store, item):
    from dataclasses import replace
    long_post = replace(item.post, text="本文\n" * 300 + "\x1b[2J終端")
    store.ingest(long_post.owner_id, [long_post])
    store.save_assessment(long_post, item.profile_id, item.assessment)
    app = ReviewApp(store, [replace(item, post=long_post)])
    async with app.run_test(size=(80, 25)) as pilot:
        await pilot.press("pagedown")
        await pilot.pause()
        assert store.post(long_post.id).text.endswith("\x1b[2J終端")


@pytest.mark.parametrize("timestamp, zone, expected", [
    ("2025-06-14T14:18:07Z", "Asia/Tokyo", "2025年06月14日 23:18:07 JST"),
    ("2024-12-31T15:00:00Z", "Asia/Tokyo", "2025年01月01日 00:00:00 JST"),
    ("2025-06-14T14:18:07Z", "UTC", "2025年06月14日 14:18:07 UTC"),
    ("2025-06-14T14:18:07Z", "America/New_York", "2025年06月14日 10:18:07 EDT"),
    ("2025-01-14T14:18:07Z", "America/New_York", "2025年01月14日 09:18:07 EST"),
])
async def test_post_date_does_not_require_non_ascii_strftime(
    store, item, monkeypatch, timestamp, zone, expected
):
    from dataclasses import replace
    import time

    native_strftime = time.strftime

    def ascii_only_strftime(format_string, *args):
        # Reproduce the failing locale boundary on every CI platform, without
        # changing the process-wide locale or requiring a Japanese locale.
        format_string.encode("ascii")
        return native_strftime(format_string, *args)

    monkeypatch.setattr(time, "strftime", ascii_only_strftime)
    post = replace(item.post, created_at=timestamp)
    store.ingest(post.owner_id, [post])
    store.save_assessment(post, item.profile_id, item.assessment)
    app = ReviewApp(store, [replace(item, post=post)], timezone_name=zone)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        body = app.query_one("#post-body", Static).content
        assert isinstance(body, str)
        assert f"投稿日時：{expected}\n" in body
        await pilot.press("n", "enter")
        await pilot.pause()
        assert store.decision(post) == "keep"
        assert store.deletion(post.id) is None
