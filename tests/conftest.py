from __future__ import annotations

import socket
from pathlib import Path

import pytest

from post_review.domain import Assessment, Item, Post, digest
from post_review.providers.jev import load_rules, profile_config
from post_review.store import Store


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not open network connections")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def post():
    return Post("1234567890123456789", "12345", "2025-06-14T14:18:00.000000+00:00", "手元の日記に書いておこう。")


@pytest.fixture
def config():
    rules = load_rules(Path(__file__).parents[1] / "rules.toml")
    return profile_config(rules, "jev-1.13.0", "original", 16000)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.sqlite3", create=True) as result:
        yield result


@pytest.fixture
def item(store, post, config):
    store.ingest(post.owner_id, [post])
    profile = store.add_profile(config)
    assessment = Assessment("DELETE_CANDIDATE", {"DELETE_CANDIDATE": 0.99, "KEEP": 0.005,
                                               "NEEDS_CONTEXT": 0.005},
                            0.8, "jev-1.13.0", digest(post.text))
    store.save_assessment(post, profile, assessment)
    return Item(post, assessment, profile)
