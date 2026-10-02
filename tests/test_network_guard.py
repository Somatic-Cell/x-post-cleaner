"""Regression tests for the offline guard; all HTTP responses are synthetic."""
import asyncio
import socket

import httpx
import pytest


@pytest.mark.parametrize("address", [("192.0.2.1", 443), ("127.0.0.1", 12345)])
@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_application_tcp_is_blocked(address, method):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        with pytest.raises(AssertionError, match="Tests must not open network connections"):
            getattr(sock, method)(address)


def test_create_connection_is_blocked():
    with pytest.raises(AssertionError, match="Tests must not open network connections"):
        socket.create_connection(("example.invalid", 443), timeout=1)


def test_python_dns_lookup_is_blocked():
    with pytest.raises(AssertionError, match="Tests must not open network connections"):
        socket.getaddrinfo("example.invalid", 443)


def test_socketpair_works_and_does_not_leave_guard_open():
    left, right = socket.socketpair()
    with left, right:
        left.settimeout(1)
        right.settimeout(1)
        left.sendall(b"x")
        assert right.recv(1) == b"x"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(AssertionError, match="Tests must not open network connections"):
                sock.connect(("127.0.0.1", 12345))


def test_asyncio_run_can_create_its_default_loop():
    async def value():
        await asyncio.sleep(0)
        return 42

    assert asyncio.run(value()) == 42


def test_real_httpx_sync_transport_is_blocked():
    with httpx.Client(trust_env=False, timeout=1) as client:
        with pytest.raises(AssertionError, match="Tests must not open network connections"):
            client.get("https://example.invalid/")


async def test_real_httpx_async_transport_is_blocked():
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        with pytest.raises(AssertionError, match="Tests must not open network connections"):
            await client.get("https://example.invalid/")


def test_httpx_mock_transport_still_works():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport, trust_env=False) as client:
        assert client.get("https://example.invalid/").json() == {"ok": True}
