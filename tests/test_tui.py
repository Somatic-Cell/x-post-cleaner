"""Headless UI tests. Requires installation with [tui]; CI installs it explicitly."""
import pytest

pytest.importorskip("textual", reason="Textual optional dependency is not installed")

from textual.widgets import Input
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
