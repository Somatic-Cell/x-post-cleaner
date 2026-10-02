from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

from post_review.domain import Assessment, Item, Post, digest
from post_review.providers.jev import load_rules, profile_config
from post_review.store import Store


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block real HTTP and application TCP/DNS, but preserve asyncio's socketpair."""
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair
    pair_state = threading.local()

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("Tests must not open network connections")

    async def forbidden_async(*args: Any, **kwargs: Any) -> NoReturn:
        forbidden()

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        # CPython on Windows constructs socketpair via a private loopback TCP
        # connection. Permit only that synchronous operation in this thread,
        # not arbitrary localhost connections or a process-wide guard bypass.
        if (getattr(pair_state, "active", False)
                and sock.family in (socket.AF_INET, socket.AF_INET6)
                and sock.type == socket.SOCK_STREAM
                and isinstance(address, tuple) and len(address) >= 2
                and address[0] in ("127.0.0.1", "::1")):
            return original_connect(sock, address)
        forbidden()

    def guarded_socketpair(*args: Any, **kwargs: Any) -> tuple[socket.socket, socket.socket]:
        previous = getattr(pair_state, "active", False)
        pair_state.active = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            pair_state.active = previous

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socketpair", guarded_socketpair)
    # Block HTTPX's real transports before they can perform DNS or I/O.
    # MockTransport does not use these methods and remains available to tests.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)


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
