from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Generator

import httpx
from oauthlib.oauth1 import Client as OAuth1Client

from ..domain import canonical_text, valid_id
from ..errors import AppError, DeleteUnknown, ProviderError
from .http import read_request, retry_deadline

BASE = "https://api.x.com/2"


class OAuth1UserAuth(httpx.Auth):
    def __init__(self, api_key: str, api_secret: str, token: str, token_secret: str):
        if not all((api_key, api_secret, token, token_secret)):
            raise AppError("missing_x_oauth1_user_credentials")
        self.signer = OAuth1Client(api_key, client_secret=api_secret,
                                   resource_owner_key=token, resource_owner_secret=token_secret)

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        # This adapter signs only GET/DELETE with no request body.
        if request.method not in {"GET", "DELETE"} or request.content:
            raise AppError("unsupported_x_signing_request")
        _, headers, _ = self.signer.sign(str(request.url), http_method=request.method,
                                         headers=dict(request.headers))
        request.headers.update(headers)
        yield request


@dataclass(frozen=True)
class LivePost:
    id: str
    author_id: str
    text: str
    flags: tuple[str, ...]


def parse_live(obj: Any) -> LivePost:
    try:
        if obj.get("errors"):
            raise ValueError
        data = obj["data"]
        post_id, author = valid_id(data["id"]), valid_id(data["author_id"])
        text = data["text"]
        if not isinstance(text, str):
            raise ValueError
        flags: set[str] = set()
        for ref in data.get("referenced_tweets", []):
            kind = ref["type"]
            if kind == "retweeted":
                flags.add("repost_or_rt_prefix")
            elif kind == "replied_to":
                flags.add("reply_context_missing")
            elif kind == "quoted":
                flags.add("quote_context_missing")
            else:
                flags.add("unknown_reference")
        if data.get("attachments"):
            flags.add("media_not_analyzed")
        if len(data.get("edit_history_tweet_ids", [])) > 1:
            flags.add("edited_post")
        if data.get("entities", {}).get("urls"):
            flags.add("linked_content_not_analyzed")
        # No undocumented long-post expansion is assumed. Mismatching text blocks deletion.
        return LivePost(post_id, author, canonical_text(text), tuple(sorted(flags)))
    except (ValueError, TypeError, KeyError, AttributeError, AppError):
        raise ProviderError("x_invalid_post_response") from None


class XClient:
    def __init__(self, client: httpx.AsyncClient, auth: OAuth1UserAuth):
        self.client, self.auth = client, auth

    async def me(self) -> str:
        response = await read_request(self.client, "GET", BASE + "/users/me", service="x", auth=self.auth)
        try:
            obj = response.json()
            if obj.get("errors"):
                raise ValueError
            return valid_id(obj["data"]["id"])
        except (KeyError, ValueError, TypeError, AppError):
            raise ProviderError("x_invalid_me_response") from None

    async def lookup(self, post_id: str) -> LivePost:
        response = await read_request(self.client, "GET", BASE + "/tweets/" + valid_id(post_id),
                                      service="x", auth=self.auth, params={"tweet.fields":
                                      "author_id,created_at,referenced_tweets,attachments,entities,edit_history_tweet_ids"})
        try:
            return parse_live(response.json())
        except ValueError:
            raise ProviderError("x_invalid_post_response") from None

    async def delete(self, post_id: str) -> float | None:
        # DELIBERATELY no HTTP retry. Losing the response does not mean losing the operation.
        try:
            response = await self.client.delete(BASE + "/tweets/" + valid_id(post_id), auth=self.auth)
        except httpx.TransportError:
            raise DeleteUnknown("x_delete_transport_unknown") from None
        deadline = retry_deadline(response)
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            raise DeleteUnknown(f"x_delete_http_{response.status_code}_unknown", retry_at=deadline)
        if not response.is_success:
            # Even a 404 is not counted as a successful deletion.
            raise ProviderError(f"x_delete_http_{response.status_code}", retry_at=deadline)
        try:
            obj = response.json()
            if obj.get("errors") or obj["data"]["deleted"] is not True:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise DeleteUnknown("x_delete_invalid_response_unknown") from None
        if deadline is not None and not math.isfinite(deadline):
            return None
        return deadline
