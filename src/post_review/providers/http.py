from __future__ import annotations

import asyncio
import math
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..errors import ProviderError


def retry_deadline(response: httpx.Response, now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    candidates: list[float] = []
    value = response.headers.get("retry-after")
    if value:
        try:
            seconds = float(value)
            candidates.append(now + max(0.0, seconds))
        except ValueError:
            try:
                candidates.append(parsedate_to_datetime(value).timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
    if response.headers.get("x-rate-limit-remaining") == "0" or response.status_code == 429:
        try:
            candidates.append(float(response.headers["x-rate-limit-reset"]) + 1.0)
        except (KeyError, ValueError):
            pass
    finite = [v for v in candidates if math.isfinite(v)]
    return max([now, *finite]) if finite else None


def http_client() -> httpx.AsyncClient:
    # Do not inherit proxy settings or follow redirects with credentials.
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False, trust_env=False)


async def read_request(client: httpx.AsyncClient, method: str, url: str, *, service: str,
                       attempts: int = 3, max_wait: float = 60.0, **kwargs: Any) -> httpx.Response:
    """Retry read/inference requests only. A retry may incur another inference charge."""
    for attempt in range(attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt + 1 == attempts:
                raise ProviderError(f"{service}_transport_error") from None
            await asyncio.sleep(2 ** attempt)
            continue
        if response.is_success:
            return response
        retriable = response.status_code == 429 or response.status_code >= 500
        deadline = retry_deadline(response)
        if retriable and attempt + 1 < attempts:
            wait = max(2 ** attempt, (deadline or 0.0) - time.time())
            if wait <= max_wait:
                await asyncio.sleep(wait)
                continue
        raise ProviderError(f"{service}_http_{response.status_code}", retry_at=deadline)
    raise ProviderError(f"{service}_retry_exhausted")
